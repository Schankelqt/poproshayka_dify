import schedule
import requests
import time
import logging
from dotenv import dotenv_values
from datetime import datetime
from users import TEAMS
from storage import load_answers_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("scheduler")

env = dotenv_values(".env")
TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN")

QUESTION_TEXT_WEEKDAY = (
    "Доброе утро! ☀️\n\n"
    "Пожалуйста, ответьте на 3 вопроса:\n"
    "1. Что делали вчера?\n"
    "2. Что планируете сегодня?\n"
    "3. Есть ли блокеры?"
)

QUESTION_TEXT_MONDAY = (
    "Доброе утро! ☀️\n\n"
    "Пожалуйста, ответьте на 3 вопроса:\n"
    "1. Что делали в пятницу?\n"
    "2. Что планируете сегодня?\n"
    "3. Есть ли блокеры?"
)

def is_weekday() -> bool:
    return datetime.utcnow().weekday() < 5  # Пн=0 ... Вс=6 (в UTC)

def send_questions():
    if not is_weekday():
        logger.info("Сегодня выходной, вопросы не рассылаем")
        return

    qtext = QUESTION_TEXT_MONDAY if datetime.utcnow().weekday() == 0 else QUESTION_TEXT_WEEKDAY
    logger.info("📤 Рассылка вопросов сотрудникам...")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for team_id, team_data in TEAMS.items():
        for chat_id, name in team_data["members"].items():
            try:
                resp = requests.post(url, json={"chat_id": chat_id, "text": qtext}, timeout=30)
                if resp.ok:
                    logger.info(f"✅ [{team_id}] Вопрос → {name} ({chat_id})")
                else:
                    logger.error(f"❌ [{team_id}] Ошибка вопроса {name} ({chat_id}): {resp.status_code} {resp.text}")
            except Exception as e:
                logger.error(f"❌ [{team_id}] Исключение вопроса {name} ({chat_id}): {e}")
            time.sleep(1)  # мягкая задержка

def build_digest(answers: dict, team_members: dict) -> str:
    if not answers:
        return "⚠️ Пока нет ответов от сотрудников."

    lines = ["📝 Статусы на отчётное время:\n"]
    total = len(team_members)
    responded = 0

    for chat_id, name in team_members.items():
        data = answers.get(str(chat_id))
        if data:
            lines.append(f"— {name}:\n{data.get('summary','')}\n")
            responded += 1
        else:
            lines.append(f"— {name}:\n- (прочерк)\n")

    lines.append(f"Отчитались: {responded}/{total}")
    return "\n".join(lines)

def send_summary(team_id: int):
    if not is_weekday():
        logger.info(f"Сегодня выходной, отчёты команде {team_id} не отправляем")
        return

    logger.info(f"📤 Отправка отчёта менеджерам команды {team_id}...")
    answers = load_answers_for()  # за сегодня (UTC дата)

    team_data = TEAMS[team_id]
    digest = build_digest(answers, team_data["members"])
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    recipients = team_data.get("managers") or [team_data.get("manager")]
    for manager_id in recipients:
        try:
            resp = requests.post(url, json={"chat_id": manager_id, "text": digest}, timeout=30)
            if resp.ok:
                logger.info(f"✅ Отчёт → менеджер {manager_id} (team {team_id})")
            else:
                logger.error(f"❌ Ошибка отчёта менеджеру {manager_id}: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"❌ Исключение отчёта менеджеру {manager_id}: {e}")

# План: всё в UTC. Если нужен часовой пояс — сдвигай время cron-а/расписания на Render.
# Рассылка вопросов в 09:00 UTC (пример)
for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
    getattr(schedule.every(), day).at("09:00").do(send_questions)

# Отчёт: команда 1 — 09:30 UTC; команда 2 — 11:00 UTC
for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
    getattr(schedule.every(), day).at("09:30").do(lambda: send_summary(1))
    getattr(schedule.every(), day).at("11:00").do(lambda: send_summary(2))

logger.info("🕒 Планировщик запущен. Ожидаем задач...")

while True:
    schedule.run_pending()
    time.sleep(30)