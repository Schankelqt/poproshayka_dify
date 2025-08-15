from flask import Flask, request
import requests, logging, json
from dotenv import dotenv_values

from users import USERS
from storage import save_answer, get_conversation_id as r_get_conv, set_conversation_id as r_set_conv

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
log = logging.getLogger(__name__)

env = dotenv_values(".env")  # локально; на Render переменные берутся из окружения
TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN")
DIFY_API_KEY = env.get("DIFY_API_KEY")
DIFY_API_URL = env.get("DIFY_API_URL", "").rstrip('/')

app = Flask(__name__)

def fetch_conversations_from_dify(chat_id: int):
    """Если conv_id не найден в Redis — спрашиваем Dify список разговоров пользователя."""
    url = f"{DIFY_API_URL}/conversations"
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}
    params = {"user": str(chat_id)}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        log.info(f"[Dify] conversations for {chat_id}: {data}")
        if data.get("data"):
            return data["data"][0]["id"]
    except Exception as e:
        log.error(f"[Dify] get conversations error: {e}")
    return None

def clean_summary(answer_text: str) -> str:
    """
    Удаляем всё выше 'sum' и саму строку 'sum', оставляем строки ниже.
    """
    if not answer_text:
        return ""
    lower = answer_text.lower()
    pos = lower.find("sum")
    if pos == -1:
        return answer_text.strip()
    tail = answer_text[pos:]
    lines = tail.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[1:]).strip()

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    log.info(f"✅ Webhook: {data}")

    if not data or "message" not in data or "text" not in data["message"]:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    user_message = data["message"]["text"]
    user_name = USERS.get(chat_id, "Неизвестный")

    # 1) conv_id: сначала из Redis, иначе из Dify
    conv_id = r_get_conv(chat_id)
    if not conv_id:
        conv_id = fetch_conversations_from_dify(chat_id)
        if conv_id:
            r_set_conv(chat_id, conv_id)

    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    def send_to_dify(payload):
        try:
            resp = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=payload, timeout=60)
            log.info(f"[Dify] Status: {resp.status_code}, Body: {resp.text}")
            return resp
        except Exception as e:
            log.error(f"[Dify] request error: {e}")
            return None

    payload = {
        "inputs": {},
        "query": user_message,
        "response_mode": "blocking",
        "user": str(chat_id),
    }
    if conv_id:
        payload["conversation_id"] = conv_id

    response = send_to_dify(payload)

    # 404: «Conversation Not Exists.» — создаём новую
    if response is not None and response.status_code == 404:
        log.info(f"[Dify] conversation {conv_id} not exists, creating new...")
        payload.pop("conversation_id", None)
        response = send_to_dify(payload)
        if response is not None and response.status_code == 200:
            new_conv_id = response.json().get("conversation_id")
            if new_conv_id:
                r_set_conv(chat_id, new_conv_id)
                log.info(f"[Dify] new conversation_id: {new_conv_id}")

    # Ответ пользователю + сохранение sum
    if response is not None and response.status_code == 200:
        answer_text = response.json().get("answer", "")
        if "sum" in answer_text.lower():
            summary = clean_summary(answer_text)
            save_answer(chat_id, user_name, summary)
            reply = summary if summary else "Итог зафиксирован."
        else:
            reply = answer_text
    else:
        reply = f"⚠️ Ошибка при обращении к Dify: {response.status_code if response else 'нет ответа'}"

    # Telegram send
    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        tg = requests.post(send_url, json={"chat_id": chat_id, "text": reply}, timeout=30)
        log.info(f"[Telegram] Status: {tg.status_code}, Body: {tg.text}")
    except Exception as e:
        log.error(f"[Telegram] send error: {e}")

    return "ok"

if __name__ == "__main__":
    # Локальный запуск (на Render стартует gunicorn из render.yaml)
    app.run(host="0.0.0.0", port=5001)