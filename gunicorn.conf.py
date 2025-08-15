# gunicorn.conf.py
workers = 1            # один воркер, чтобы задачи не дублировались
threads = 4
timeout = 120
bind = "0.0.0.0:10000"  # Render сам подставит PORT, игнорируется gunicorn'ом
# Render передаёт порт через $PORT — gunicorn это понимает сам