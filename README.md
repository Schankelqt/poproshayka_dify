# Poproshayka Dify (Render)

## Сервисы
- Web (Flask, Gunicorn): принимает Telegram webhook по пути `/<TELEGRAM_TOKEN>`
- Worker (scheduler): рассылает вопросы и отправляет отчёты

## Переменные окружения
- TELEGRAM_TOKEN
- DIFY_API_KEY
- DIFY_API_URL (default: https://api.dify.ai/v1)
- REDIS_URL
- DATABASE_URL

## Деплой
1. Запушить репозиторий с `render.yaml`.
2. В Render: **New + Blueprint** → выбрать этот репозиторий.
3. Заполнить секреты (env vars) для обоих сервисов.
4. После деплоя веб‑сервиса: установить Telegram Webhook:
curl -F “url=https:///${TELEGRAM_TOKEN}” 
https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook
(или укажи `https://<your-domain>/${TELEGRAM_TOKEN}` если у тебя кастомный домен).

## Локальный запуск
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
И отправить тестовый POST:
curl -X POST http://127.0.0.1:5001/${TELEGRAM_TOKEN} 
-H “Content-Type: application/json” 
-d ‘{“message”:{“chat”:{“id”:775766895},“text”:“привет”}}’