# lendings-killer

AI website builder SaaS — мастер красоты отвечает на 6 вопросов в чате и получает готовый сайт-визитку через 30–60 секунд.

**Прод:** [dum-e.com](https://dum-e.com) · **Сервер:** `92.38.48.227` · **GitHub:** `freeezxes/lendings-killer`

Полная документация → **[AGENTS.md](AGENTS.md)**

---

## Стек

- **FastAPI** + Jinja2 templates
- **SQLite** (`lendings.db`) — users, sites, sessions, token_log, payments
- **Anthropic Bedrock** Haiku 4.5 — генерирует HTML сайт
- **Kaspi Pay** через kaspi-pos прокси (`92.38.49.113:4001`)
- **bcrypt** — пароли
- **google-auth** + **httpx** — Google OAuth 2.0
- **Pillow** — ресайз фото при загрузке

---

## Структура проекта

```
lendings-killer/
├── main.py              # FastAPI app — все роуты, onboarding, AI генерация, платежи
├── db.py                # SQLite слой — users, sites, sessions, token_log, payments
├── templates/
│   ├── landing.html     # Главная (публичная)
│   ├── auth.html        # Регистрация / вход
│   ├── index.html       # Чат-онбординг (создать сайт)
│   ├── dashboard.html   # Личный кабинет пользователя
│   ├── profile.html     # Профиль + история токенов
│   ├── payment.html     # Покупка слотов / кредитов
│   └── admin.html       # Админ-панель
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
Environment=GOOGLE_CLIENT_ID=<google-oauth-client-id>
Environment=GOOGLE_CLIENT_SECRET=<google-oauth-client-secret>
Environment=GOOGLE_REDIRECT_URI=https://dum-e.com/auth/google/callback
```

Токен истекает — при ошибке Bedrock обновить значение в сервис-файле и сделать `sudo systemctl daemon-reload && sudo systemctl restart lendings`.

Google OAuth необязателен: если переменные `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` не заданы или пакет `google-auth` не установлен, обычный вход по телефону продолжает работать, а кнопка Google не показывается.

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
pip install fastapi uvicorn anthropic httpx google-auth requests pillow bcrypt jinja2 python-multipart aiofiles
export AWS_BEARER_TOKEN_BEDROCK=<token>
export AWS_REGION=us-east-1
export GOOGLE_CLIENT_ID=<local-client-id>
export GOOGLE_CLIENT_SECRET=<local-client-secret>
export GOOGLE_REDIRECT_URI=http://127.0.0.1:8002/auth/google/callback
uvicorn main:app --reload --port 8002
```

---

## Google OAuth 2.0

Google вход является дополнительным способом авторизации. Существующие аккаунты по телефону и bcrypt-паролю, сессии, сайты и платежи не меняются.

### Настройка Google Cloud Console

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте проект или выберите существующий.
3. Включите OAuth consent screen, укажите название приложения и домен `dum-e.com`.
4. В разделе Credentials создайте `OAuth client ID`.
5. Application type: `Web application`.
6. Добавьте Authorized redirect URIs:
   - `http://127.0.0.1:8002/auth/google/callback` для локального теста
   - `https://dum-e.com/auth/google/callback` для продакшена
7. Сохраните `Client ID` и `Client Secret`.

### Локальное тестирование

```bash
export GOOGLE_CLIENT_ID=<local-client-id>
export GOOGLE_CLIENT_SECRET=<local-client-secret>
export GOOGLE_REDIRECT_URI=http://127.0.0.1:8002/auth/google/callback
python -m uvicorn main:app --reload --port 8002
```

Откройте `http://127.0.0.1:8002/auth`. Если все переменные заданы и `google-auth` установлен, появится кнопка `Continue with Google`.

### Продакшен

В `/etc/systemd/system/lendings.service` добавьте:

```ini
Environment=GOOGLE_CLIENT_ID=<production-client-id>
Environment=GOOGLE_CLIENT_SECRET=<production-client-secret>
Environment=GOOGLE_REDIRECT_URI=https://dum-e.com/auth/google/callback
```

Затем примените изменения:

```bash
sudo systemctl daemon-reload
sudo systemctl restart lendings
```

Убедитесь, что nginx проксирует HTTPS и передаёт `X-Forwarded-Proto https`, чтобы cookies выставлялись с `Secure` в продакшене.

### Поведение аккаунтов

- Новый Google пользователь создаётся с `auth_provider='google'`, `password=NULL`, `tokens=0`.
- Если найден пользователь с таким же `google_id`, создаётся новая сессия для него.
- Если найден локальный пользователь с таким же подтверждённым email, Google логин привязывается к этому аккаунту без перезаписи пароля.
- Если `google_id` и email указывают на разных пользователей, вход отклоняется как `account_conflict`.

### Troubleshooting

| Ошибка | Что проверить |
|--------|---------------|
| `google_not_configured` | Заданы ли все три env vars и установлен ли `google-auth` |
| `invalid_state` | Не устарела ли вкладка входа, не блокируются ли cookies |
| `invalid_code` | Совпадает ли `GOOGLE_REDIRECT_URI` с URI в Google Console |
| `email_not_verified` | Подтверждён ли email в Google аккаунте |
| `account_conflict` | Нет ли разных пользователей с одним email / Google ID |
| Кнопка Google не видна | Проверьте `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, пакет `google-auth` и рестарт сервиса |

---

## Бизнес-логика

### Онбординг (чат до генерации)

AI (`CHAT_SYSTEM`) собирает через диалог:
1. Имя / профессия
2. Услуги и цены
3. Город и контакт (WhatsApp/Telegram/телефон)
4. Вайб / ссылка на референс-сайт (опционально)

После сбора всех данных — AI генерирует полный HTML и сохраняет в `generated_sites/<slug>.html`.

### Платёжная модель

| Пакет                  | Цена   | Даёт              |
|------------------------|--------|-------------------|
| 1 сайт                 | 5 000₸ | +1 слот +1000 кредитов |
| 500 кредитов           | 990₸   | +500 кредитов     |
| 1 500 кредитов         | 2 490₸ | +1500 кредитов    |

Новый пользователь → сразу на страницу оплаты (0 слотов).

### Токены

| Событие               | Изменение                              |
|----------------------|----------------------------------------|
| Генерация сайта       | −1 кредит на каждые ~1K claude tokens |
| Редактирование сайта  | −1 кредит на каждые ~1K claude tokens |
| Admin grant           | +N кредитов вручную                    |

### Дизайн-бриф с референса

Если пользователь даёт URL на чужой сайт — `_fetch_url()` скачивает CSS, вытаскивает цвета / шрифты / CSS-переменные и передаёт в промпт как «дизайн-бриф».

---

## Роуты

| Метод | URL                           | Описание                                  |
|-------|-------------------------------|-------------------------------------------|
| GET   | `/`                           | Лендинг                                   |
| GET   | `/auth`                       | Страница входа/регистрации                |
| POST  | `/auth/register`              | Регистрация                               |
| POST  | `/auth/login`                 | Вход                                      |
| GET   | `/auth/google`                | Старт Google OAuth                        |
| GET   | `/auth/google/callback`       | Callback Google OAuth                     |
| POST  | `/auth/logout`                | Выход                                     |
| GET   | `/create`                     | Чат-онбординг                             |
| GET   | `/dashboard`                  | Личный кабинет                            |
| GET   | `/profile`                    | Профиль + история токенов                 |
| GET   | `/site/{slug}`                | Просмотр сгенерированного сайта           |
| POST  | `/site/{slug}/edit`           | Редактирование сайта через чат            |
| POST  | `/site/{slug}/delete`         | Удалить сайт                              |
| GET   | `/start`                      | Начало чата (приветствие)                 |
| POST  | `/chat`                       | Шаг онбординга / запуск генерации         |
| POST  | `/upload-photo`               | Загрузка фото                             |
| GET   | `/payment`                    | Страница оплаты                           |
| POST  | `/payment/create`             | Создать инвойс Kaspi                      |
| GET   | `/payment/status/{order_id}`  | Статус оплаты                             |
| POST  | `/payment/webhook`            | Webhook от kaspi-pos (HMAC)               |
| GET   | `/admin`                      | Админ-панель (phone: `77064177628`)       |
| GET   | `/admin/api/stats`            | Статистика JSON                           |
| GET   | `/admin/api/users`            | Список пользователей                      |
| GET   | `/admin/api/user/{uid}`       | Детали пользователя                       |
| POST  | `/admin/api/user/{uid}/add-tokens` | Начислить токены вручную            |

---

## База данных

```sql
users       — id, phone, password (bcrypt, nullable для Google), email, google_id,
              auth_provider, avatar_url, name, tokens, site_slots, created
sites       — id, user_id, slug, title, data (JSON), html_path, tokens_used,
              chat_in, chat_out, gen_in, gen_out, cache_read, cost_usd, created, updated
token_log   — id, user_id, site_id, delta, reason, claude_in, claude_out, cache_read, cost_usd, ts
payments    — id, user_id, order_id, invoice_id, amount, tokens, catalog_item_id, status, created
sessions    — id (hex), user_id, expires
```

Миграции выполняются при старте через `init_db()`: OAuth-колонки добавляются только если их нет, уникальность email/google_id обеспечивается индексами, существующие данные не удаляются и таблицы не пересоздаются.
