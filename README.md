# lendings-killer

AI website builder SaaS — мастер красоты отвечает на 6 вопросов в чате и получает готовый сайт-визитку через 30 секунд.

**Прод:** [dum-e.com](https://dum-e.com) · **Сервер:** `92.38.48.227` · **GitHub:** `freeezxes/lendings-killer`

---

## Стек

- **FastAPI** + Jinja2 templates
- **SQLite** (`lendings.db`) — users, sites, sessions, token_log
- **Anthropic Bedrock** Haiku 4.5 — генерирует HTML сайт
- **bcrypt** — пароли
- **Pillow** — ресайз фото при загрузке

---

## Структура проекта

```
lendings-killer/
├── main.py              # FastAPI app — все роуты, onboarding, AI генерация
├── db.py                # SQLite слой — users, sites, sessions, token_log
├── templates/
│   ├── landing.html     # Главная (публичная)
│   ├── auth.html        # Регистрация / вход
│   ├── index.html       # Чат-онбординг (создать сайт)
│   ├── dashboard.html   # Личный кабинет пользователя
│   ├── admin.html       # Админ-панель
│   ├── site_1.html      # Шаблон 1 (не используется — сайты генерирует AI)
│   ├── site_2.html
│   └── site_3.html
├── static/
│   └── uploads/         # Фото пользователей (не в git)
├── generated_sites/     # Готовые HTML сайты клиентов (не в git)
├── idea.md              # Бизнес-идея и тарифная модель
├── costs.json           # Лог расходов на AI (не в git)
└── lendings.db          # База данных (не в git)
```

---

## Сервер

| Параметр     | Значение                                              |
|-------------|-------------------------------------------------------|
| IP           | `92.38.48.227`                                        |
| Домен        | `dum-e.com`                                           |
| SSH          | `ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227`        |
| App dir      | `/opt/lendings/`                                      |
| Uvicorn port | `127.0.0.1:8002` (за nginx)                           |
| Systemd unit | `lendings.service`                                    |
| Nginx config | `/etc/nginx/sites-available/dum-e.conf`               |

---

## Переменные окружения (systemd)

Хранятся в `/etc/systemd/system/lendings.service`:

```ini
Environment=AWS_BEARER_TOKEN_BEDROCK=<base64-token>
Environment=AWS_REGION=us-east-1
```

Токен истекает — при ошибке Bedrock обновить значение в сервис-файле и сделать `sudo systemctl daemon-reload && sudo systemctl restart lendings`.

---

## Деплой (локалка → прод)

```bash
rsync -av \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='generated_sites/' --exclude='lendings.db' \
  --exclude='static/uploads/' --exclude='costs.json' \
  -e "ssh -i ~/.ssh/id_ed25519" \
  ~/Documents/GitHub/lendings-killer/ deploy@92.38.48.227:/opt/lendings/

ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo systemctl restart lendings"
```

---

## Управление сервисом

```bash
# Перезапуск
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo systemctl restart lendings"

# Логи в реальном времени
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo journalctl -u lendings -f"

# Статус
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo systemctl status lendings"
```

---

## Локальный запуск

```bash
cd ~/Documents/GitHub/lendings-killer
python -m venv venv && source venv/bin/activate
pip install fastapi uvicorn anthropic httpx pillow bcrypt jinja2 python-multipart aiofiles
export AWS_BEARER_TOKEN_BEDROCK=<token>
export AWS_REGION=us-east-1
uvicorn main:app --reload --port 8002
```

---

## Бизнес-логика

### Онбординг (6 шагов в чате)

1. Имя / профессия
2. Услуги и цены
3. Город и контакт (WhatsApp/Telegram/телефон)
4. Фото работ (загрузка или «пропустить»)
5. Вайб / ссылка на референс-сайт
6. Дополнительные пожелания

После последнего шага — AI генерирует полный HTML и сохраняет в `generated_sites/<slug>.html`.

### Токены

| Событие               | Изменение            |
|----------------------|----------------------|
| Регистрация           | +500 токенов         |
| Генерация сайта       | −1 токен на ~1K claude tokens |

### Дизайн-бриф с референса

Если пользователь даёт URL на чужой сайт — `_fetch_url()` скачивает CSS, вытаскивает цвета / шрифты / CSS-переменные и передаёт в промпт как «дизайн-бриф».

---

## Роуты

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/` | Лендинг |
| GET | `/auth` | Страница входа/регистрации |
| POST | `/auth/register` | Регистрация |
| POST | `/auth/login` | Вход |
| POST | `/auth/logout` | Выход |
| GET | `/create` | Чат-онбординг |
| GET | `/dashboard` | Личный кабинет |
| GET | `/site/{slug}` | Просмотр сгенерированного сайта |
| POST | `/chat` | Шаг онбординга / запуск генерации |
| POST | `/upload-photo` | Загрузка фото |
| GET | `/admin` | Админ-панель (phone: `77777777777`) |
| GET | `/admin/api/stats` | Статистика JSON |

---

## База данных

```sql
users       — id, phone, password (bcrypt), name, tokens, created
sites       — id, user_id, slug, title, data (JSON), html_path, tokens_used, created
token_log   — id, user_id, site_id, delta, reason, claude_in, claude_out, cache_read, cost_usd, ts
sessions    — id (hex), user_id, expires
```
