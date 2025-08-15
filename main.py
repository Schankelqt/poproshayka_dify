from flask import Flask, request
import requests, json, os, logging
from dotenv import dotenv_values
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, date
from users import USERS, TEAMS

import redis
import psycopg2
import psycopg2.extras

# ---------- логирование ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poproshayka")

# ---------- ENV ----------
env = {**dotenv_values(".env"), **os.environ}
TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN")
DIFY_API_KEY   = env.get("DIFY_API_KEY")
DIFY_API_URL   = (env.get("DIFY_API_URL") or "").rstrip("/")
REDIS_URL      = env.get("REDIS_URL")
DATABASE_URL   = env.get("DATABASE_URL")
TZ             = pytz.timezone(os.getenv("TZ", "Europe/Moscow"))

# ---------- Flask ----------
app = Flask(__name__)

# ---------- Redis ----------
rds = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

def redis_key_for(d: date) -> str:
    return f"answers:{d.isoformat()}"  # Hash: field=str(chat_id) -> JSON {"name","summary"}

def clear_today_answers():
    if not rds: 
        return
    rds.delete(redis_key_for(datetime.now(TZ).date()))

def save_answer_to_redis(chat_id: int, name: str, summary: str):
    if not rds:
        return
    key = redis_key_for(datetime.now(TZ).date())
    rds.hset(key, str(chat_id), json.dumps({"name": name, "summary": summary}, ensure_ascii=False))

def load_answers_from_redis(for_date: date) -> dict:
    """Возвращает dict[str(chat_id)] = {"name","summary"}"""
    if not rds:
        return {}
    key = redis_key_for(for_date)
    raw = rds.hgetall(key)
    out = {}
    for k, v in raw.items():
        try:
            out[k] = json.loads(v)
        except Exception:
            pass
    return out

# ---------- Postgres ----------
pg_conn = None
if DATABASE_URL:
    pg_conn = psycopg2.connect(DATABASE_URL)
    pg_conn.autocommit = True

def pg_init():
    if not pg_conn:
        return
    with pg_conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS answers (
          id          bigserial PRIMARY KEY,
          day         date        NOT NULL,
          chat_id     bigint      NOT NULL,
          user_name   text        NOT NULL,
          summary     text        NOT NULL,
          created_at  timestamptz NOT NULL DEFAULT now(),
          updated_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_answers_day_chat
          ON answers(day, chat_id);
        """)
pg_init()

def pg_upsert_answer(day: date, chat_id: int, user_name: str, summary: str):
    if not pg_conn:
        return
    with pg_conn.cursor() as cur:
        cur.execute("""
        INSERT INTO answers(day, chat_id, user_name, summary)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (day, chat_id)
        DO UPDATE SET user_name = EXCLUDED.user_name,
                      summary   = EXCLUDED.summary,
                      updated_at= now();
        """, (day, chat_id, user_name, summary))

# ---------- Dify helpers ----------
def tg_send(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text})
    if not r.ok:
        log.error("Telegram send error %s: %s", r.status_code, r.text)
    return r

conversation_ids = {}  # { chat_id: conversation_id }

def get_conversation_id(chat_id):
    try:
        url = f"{DIFY_API_URL}/conversations"
        headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}
        params = {"user": str(chat_id)}
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        log.info("[Dify] conversations for %s: %s", chat_id, data)
        if data.get("data"):
            return data["data"][0]["id"]
    except Exception as e:
        log.error("get_conversation_id error for %s: %s", chat_id, e)
    return None

def send_to_dify(payload):
    try:
        headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
        url = f"{DIFY_API_URL}/chat-messages"
        log.info("[Dify] request: %s", json.dumps(payload, ensure_ascii=False))
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        log.info("[Dify] status=%s body=%s", r.status_code, r.text)
        return r
    except Exception as e:
        log.error("send_to_dify exception: %s", e)
        return None

def extract_summary(answer_text: str) -> str | None:
    lower = answer_text.lower()
    pos = lower.find("sum")
    if pos == -1:
        return None
    after = answer_text[pos:]
    lines = after.splitlines()
    if lines:
        return "\n".join(lines[1:]).strip()
    return answer_text[pos:].strip()

# ---------- Telegram webhook ----------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    log.info("Webhook data: %s", data)

    if not (data and "message" in data and "text" in data["message"]):
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    user_msg = data["message"]["text"]
    user_name = USERS.get(chat_id, "Неизвестный")

    conv_id = conversation_ids.get(chat_id)
    if not conv_id:
        conv_id = get_conversation_id(chat_id)
        if conv_id:
            conversation_ids[chat_id] = conv_id
        else:
            log.info("No conversation for %s, will create new", chat_id)

    payload = {
        "inputs": {},
        "query": user_msg,
        "response_mode": "blocking",
        "user": str(chat_id),
    }
    if conv_id:
        payload["conversation_id"] = conv_id

    resp = send_to_dify(payload)

    if resp is not None and resp.status_code == 404:
        payload.pop("conversation_id", None)
        resp = send_to_dify(payload)
        if resp is not None and resp.status_code == 200:
            new_conv = resp.json().get("conversation_id")
            if new_conv:
                conversation_ids[chat_id] = new_conv
                log.info("New conversation for %s: %s", chat_id, new_conv)

    if resp is not None and resp.status_code == 200:
        answer = resp.json().get("answer", "")
        summary = extract_summary(answer)
        if summary:
            # сохраняем в Redis (день): и в Postgres (история)
            today = datetime.now(TZ).date()
            save_answer_to_redis(chat_id, user_name, summary)
            pg_upsert_answer(today, chat_id, user_name, summary)
            tg_send(chat_id, summary)
        else:
            tg_send(chat_id, answer)
    else:
        tg_send(chat_id, f"⚠️ Ошибка при обращении к Dify: {resp.status_code if resp else 'нет ответа'}")

    return "ok"

@app.route("/healthz")
def healthz():
    return "ok", 200

# ---------- Рассылка/отчёты ----------
QUESTION_TEXT_WORKDAY = (
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

def send_questions():
    weekday = datetime.now(TZ).weekday()  # Mon=0..Sun=6
    text = QUESTION_TEXT_MONDAY if weekday == 0 else QUESTION_TEXT_WORKDAY
    log.info("Рассылка вопросов (weekday=%s)", weekday)

    clear_today_answers()  # очищаем дневной буфер

    for team_id, team_data in TEAMS.items():
        for chat_id, name in team_data["members"].items():
            try:
                r = tg_send(chat_id, text)
                if r.ok:
                    log.info("Вопрос отправлен: %s (%s)", name, chat_id)
                else:
                    log.error("Ошибка отправки вопроса %s (%s): %s %s",
                              name, chat_id, r.status_code, r.text)
            except Exception as e:
                log.error("Исключение при отправке вопроса %s (%s): %s", name, chat_id, e)
            import time; time.sleep(1)

def build_digest(team_members: dict, for_date: date) -> str:
    answers = load_answers_from_redis(for_date)
    lines = ["📝 Статусы на отчётное время:\n"]
    total = len(team_members)
    responded = 0

    for chat_id, name in team_members.items():
        payload = answers.get(str(chat_id))
        if payload:
            lines.append(f"— {name}:\n{payload.get('summary','')}\n")
            responded += 1
        else:
            lines.append(f"— {name}:\n- (прочерк)\n")

    lines.append(f"Отчитались: {responded}/{total}")
    return "\n".join(lines)

def send_summary(team_id: int):
    log.info("Отправка отчёта команде %s", team_id)
    today = datetime.now(TZ).date()
    digest = build_digest(TEAMS[team_id]["members"], today)
    managers = TEAMS[team_id].get("managers") or [TEAMS[team_id].get("manager")]
    for mid in managers:
        try:
            r = tg_send(mid, digest)
            if r.ok:
                log.info("Отчёт отправлен менеджеру %s (team %s)", mid, team_id)
            else:
                log.error("Ошибка отправки отчёта менеджеру %s: %s %s", mid, r.status_code, r.text)
        except Exception as e:
            log.error("Исключение при отправке отчёта менеджеру %s: %s", mid, e)

# ---------- Планировщик ----------
def start_scheduler():
    sched = BackgroundScheduler(timezone=TZ)
    # вопросы Пн‑Пт 09:00
    sched.add_job(send_questions, CronTrigger(day_of_week="mon-fri", hour=9,  minute=0))
    # отчёты: команде 1 — 09:30; команде 2 — 11:00
    sched.add_job(lambda: send_summary(1), CronTrigger(day_of_week="mon-fri", hour=9,  minute=30))
    sched.add_job(lambda: send_summary(2), CronTrigger(day_of_week="mon-fri", hour=11, minute=0))
    sched.start()
    log.info("APScheduler started")

start_scheduler()