# users.py

TEAMS = {
    1: {
        "members": {
            # Пример
            775766895: "Кирилл Востриков",
            731869173: "Татьяна Воронкова",
        },
        "managers": [728631150, 775766895],  # можно список
    },
    2: {
        "members": {
            8134384275: "Кирилл 2",
        },
        "managers": [8134384275],
    }
}

# Плоский словарь chat_id -> name для main.py
USERS = {}
for team in TEAMS.values():
    USERS.update(team["members"])