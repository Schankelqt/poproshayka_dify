from flask import Flask, request
import requests, json, os, re, time, logging
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, date
from users import USERS, TEAMS

# ====== env & logging ======
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poproshayka")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
DIFY_API_KEY   = os.getenv("DIFY_API_KEY", "")
DIFY_API_URL   = os.getenv("DIFY_API_URL", "").rstrip("/")
TZ             = os.getenv("TZ", "Europe/Moscow")

REDIS_URL      = os.getenv("REDIS_URL", "")      # redis://default:pass@host:port
DATABASE_URL   = os.getenv("DATABASE_URL", "")   # postgres://... or postgresql://...

if not all([TELEGRAM_TOKEN, DIFY_API_KEY, DIFY_API_URL]):
    log.warning("Не заданы TELEGRAM_TOKEN/DIFY_API_KEY/DIFY_API_URL")

# ====== Flask ======
app = Flask(__name__)

# ====== Redis ======
from redis import Redis
redis = Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

def rget(key, default=None):
    try:
        if redis: 
            v = redis.get(key)
            return v if v is not None else default
    except Exception as e:
        log.error(f"Redis error get({key}): {e}")
    return default

def rset(key, val, ex=None):
    try:
        if redis:
            redis.set(key, val, ex=ex)
    except Exception as e:
        log.error(f"Redis error set({key}): {e}")

def rdel_pattern(pattern):
    if not redis: return
    try:
        for k in redis.scan_iter(pattern):
            redis.delete(k)
    except Exception as e:
        log.error(f"Redis scan/del {pattern} error: {e}")

# ====== Postgres via SQLAlchemy ======
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True) if engine else None

def init_db():
    if not engine: 
        log.warning("DATABASE_URL не задан — история в БД вестись не будет.")
        return
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS answers (
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          user_name TEXT NOT NULL,
          summary TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """))
    log.info("DB: миграции применены / таблица answers готова")

def save_answer_to_db(user_id: int, user_name: str, summary: str):
    if not SessionLocal: return
    try:
        with SessionLocal() as s:
            s.execute(
                text("INSERT INTO answers (user_id, user_name, summary) VALUES (:u, :n, :s)"),
                {"u": int(user_id), "n": user_name, "s": summary}
            )
            s.commit()
    except Exception as e:
        log.error(f"DB insert error: {e}")

# ====== Вспомогательное ======
def tg_send(chat_id: int, text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": chat_id, "text": text})
        if not r.ok:
            log.error(f"TG send fail to {chat_id}: {r.status_code} {r.text}")
        return r.ok
    except Exception as e:
        log.error(f"TG send exception to {chat_id}: {e}")
        return False

def get_conversation_id(chat_id: int):
    """Берём свежий conversation из Dify или None."""
    try:
        url = f"{DIFY_API_URL}/conversations"
        headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}
        params  = {"user": str(chat_id)}
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        conv = (data.get("data") or [])
        return conv[0]["id"] if conv else None
    except Exception as e:
        log.error(f"get_conversation_id error for {chat_id}: {e}")
        return None

def dify_chat(chat_id: int, text_query: str, conversation_id: str | None):
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "inputs": {},
        "query": text_query,
        "response_mode": "blocking",
        "user": str(chat_id)
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    def _post(p):
        r = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=p, timeout=60)
        log.info(f"[Dify] status={r.status_code}, body={r.text[:500]}")
        return r

    r = _post(payload)
    if r.status_code == 404:
        # Conversation Not Exists — создаём новую
        payload.pop("conversation_id", None)
        r = _post(payload)

    return r

def cut_summary(answer_text: str) -> str | None:
    """
    Удаляем всё до строки с 'sum' включительно. Если 'sum' нет — None.
    """
    lower = answer_text.lower()
    # ищем слово 'sum' как отдельную "строчку/заголовок"
    m = re.search(r"(^|\n)\s*sum[:\s]*\n?", lower)
    if not m:
        return None
    start = m.end()  # позиция сразу после строки 'sum...'
    return answer_text[start:].strip()

# ====== Webhook ======
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    log.info(f"Webhook: {data}")

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text_query = data["message"]["text"]
        user_name = USERS.get(chat_id, "Неизвестный")

        # conversation_id — держим в Redis
        conv_id = rget(f"conv:{chat_id}")
        if not conv_id:
            conv_id = get_conversation_id(chat_id)
            if conv_id:
                rset(f"conv:{chat_id}", conv_id, ex=60*60*24*7)  # неделя

        resp = dify_chat(chat_id, text_query, conv_id)
        if not resp or not resp.ok:
            tg_send(chat_id, f"⚠️ Ошибка при обращении к Dify: {resp.status_code if resp else 'нет ответа'}")
            return "ok"

        answer_text = resp.json().get("answer", "")
        # если пришла финалка с 'sum' — режем и сохраняем
        summary = cut_summary(answer_text)
        if summary:
            # сохраняем «за сегодня» в Redis
            rset(f"answer:{chat_id}", json.dumps({"name": user_name, "summary": summary}), ex=60*60*24*2)
            # в вечную историю — Postgres
            save_answer_to_db(chat_id, user_name, summary)
            # сотруднику можно показать только summary, если хочешь — или весь ответ:
            tg_send(chat_id, summary)
        else:
            # промежуточные реплики — просто проксируем
            tg_send(chat_id, answer_text)
    return "ok"

@app.route("/healthz")
def healthz():
    return "ok", 200

# ====== Планировщик ======
def is_weekday():
    return datetime.now().weekday() < 5  # Пн..Пт

QUESTION_TEXT_WEEKDAY = (
    "Доброе утро! ☀️\n\n"
    "Ответьте, пожалуйста, на 3 вопроса:\n"
    "1. Что делали вчера?\n"
    "2. Что планируете сегодня?\n"
    "3. Есть ли блокеры?"
)
QUESTION_TEXT_MONDAY = (
    "Доброе утро! ☀️\n\n"
    "Ответьте, пожалуйста, на 3 вопроса:\n"
    "1. Что делали в ПЯТНИЦУ?\n"
    "2. Что планируете сегодня (понедельник)?\n"
    "3. Есть ли блокеры?"
)

def broadcast_questions():
    if not is_weekday():
        log.info("Выходной — рассылку вопросов пропускаем")
        return

    # очищаем ответы за сегодня (только кэш), историю в БД не трогаем
    rdel_pattern("answer:*")

    text_to_send = QUESTION_TEXT_MONDAY if datetime.now().weekday() == 0 else QUESTION_TEXT_WEEKDAY

    for team_id, team in TEAMS.items():
        for chat_id, name in team["members"].items():
            ok = tg_send(chat_id, text_to_send)
            if ok:
                log.info(f"[Q] sent to {name} ({chat_id}) team={team_id}")
            else:
                log.error(f"[Q] FAIL to {name} ({chat_id}) team={team_id}")
            time.sleep(1)  # маленькая пауза, чтобы не уткнуться в лимиты

def build_digest_for_team(team_members: dict[int, str]) -> str:
    lines = ["📝 Статусы на отчётное время:\n"]
    total = len(team_members)
    responded = 0

    for chat_id, name in team_members.items():
        raw = rget(f"answer:{chat_id}")
        if raw:
            data = json.loads(raw)
            summary = data.get("summary", "")
            lines.append(f"— {name}:\n{summary}\n")
            responded += 1
        else:
            lines.append(f"— {name}:\n- (прочерк)\n")

    lines.append(f"Отчитались: {responded}/{total}")
    return "\n".join(lines)

def send_summary(team_id: int):
    if not is_weekday():
        log.info(f"Выходной — отчёт для команды {team_id} пропускаем")
        return

    team = TEAMS[team_id]
    digest = build_digest_for_team(team["members"])
    for manager_id in team.get("managers", []):
        ok = tg_send(manager_id, digest)
        if ok:
            log.info(f"[S] sent summary to manager {manager_id} (team {team_id})")
        else:
            log.error(f"[S] FAIL summary to manager {manager_id} (team {team_id})")

# запускаем планировщик внутри веб-сервиса
scheduler = BackgroundScheduler(timezone=TZ)
# вопросы всем командам — 09:00 по будням
scheduler.add_job(broadcast_questions, CronTrigger(day_of_week='mon-fri', hour=9, minute=0, timezone=TZ))
# отчёты: команда 1 — 09:30; команда 2 — 11:00
scheduler.add_job(send_summary, CronTrigger(day_of_week='mon-fri', hour=9, minute=30, timezone=TZ), args=[1])
scheduler.add_job(send_summary, CronTrigger(day_of_week='mon-fri', hour=11, minute=0, timezone=TZ), args=[2])
scheduler.start()
log.info("APScheduler started")

# ====== старт приложения ======
with app.app_context():
    init_db()
    log.info("App ready")