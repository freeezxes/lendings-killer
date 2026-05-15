# AGENTS.md — lendings-killer

Полная документация для AI-агентов и разработчиков: архитектура, бизнес-логика, API, деплой.

---

## Что это

AI SaaS для создания сайтов-визиток. Малый бизнес (барберы, мастера маникюра, репетиторы, массажисты) отвечает на вопросы в чате — через 30–60 секунд получает готовый HTML-сайт с красивым дизайном, адаптированным под их нишу.

**Прод:** `https://dum-e.com` (временный домен, цель — `lendings.kz`)

---

## Архитектура

```
FastAPI (main.py)
    ├── SessionMiddleware  — cookie-based auth (sid)
    ├── /chat              — AI-онбординг + генерация сайта
    ├── /site/{slug}/edit  — AI-редактирование готового сайта
    ├── /payment/*         — Kaspi Pay через kaspi-pos прокси
    └── /admin/*           — внутренняя аналитика

db.py — SQLite (синхронный, sqlite3)
    ├── users, sites, sessions, token_log, payments

AI: Anthropic Bedrock Haiku 4.5
    ├── CHAT_SYSTEM      — онбординг-диалог (JSON-ответы, ready:bool)
    ├── EDIT_CHAT_SYSTEM — диалог правок (JSON-ответы, ready:bool)
    └── SYSTEM_PROMPT    — генерация HTML (кешируется как ephemeral)
```

---

## Файловая структура

```
lendings-killer/
├── main.py              # Весь FastAPI — роуты, AI, платежи
├── db.py                # SQLite CRUD
├── templates/
│   ├── landing.html     # Публичный лендинг
│   ├── auth.html        # Вход / регистрация
│   ├── index.html       # Чат-онбординг (create page)
│   ├── dashboard.html   # Личный кабинет
│   ├── profile.html     # Профиль + история токенов
│   ├── payment.html     # Страница оплаты
│   ├── admin.html       # Админка
│   ├── site_1/2/3.html  # Референс-шаблоны (не используются в прод)
├── static/
│   └── uploads/         # Фото пользователей (не в git)
├── generated_sites/     # Готовые HTML-файлы клиентов (не в git)
├── idea.md              # Бизнес-концепция и тарифная модель
├── costs.json           # Лог AI-расходов (не в git)
├── lendings.db          # SQLite база (не в git)
└── .gitignore
```

---

## База данных

```sql
-- Пользователи
users (
    id            INTEGER PRIMARY KEY,
    phone         TEXT UNIQUE,       -- только цифры, нормализован при регистрации
    password      TEXT,              -- bcrypt hash (NULL для Google OAuth пользователей)
    name          TEXT,
    email         TEXT,              -- из Google профиля
    google_id     TEXT UNIQUE,       -- sub из Google ID token
    auth_provider TEXT,              -- 'local' или 'google'
    avatar_url    TEXT,              -- picture из Google профиля
    tokens        INTEGER DEFAULT 0, -- кредиты для генерации/правок
    site_slots    INTEGER DEFAULT 0, -- сколько сайтов может создать
    created       TEXT
)

-- Сайты
sites (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER,
    slug        TEXT UNIQUE,       -- URL-slug, транслитерирован из имени клиента
    title       TEXT,              -- имя/профессия клиента
    data        TEXT,              -- JSON: name, services, city, vibe, photo_urls, chat_history
    html_path   TEXT,              -- путь к generated_sites/<slug>.html
    tokens_used INTEGER,
    chat_in/out INTEGER,           -- токены разговора с AI
    gen_in/out  INTEGER,           -- токены генерации HTML
    cache_read  INTEGER,
    cost_usd    REAL,
    created/updated TEXT
)

-- Лог токенов (дебет/кредит)
token_log (
    id, user_id, site_id,
    delta       INTEGER,           -- отрицательный при списании
    reason      TEXT,              -- "site_generate:slug", "admin_grant", "slot_purchase:order_id"
    claude_in, claude_out, cache_read INTEGER,
    cost_usd    REAL,
    ts          TEXT
)

-- Платежи (Kaspi)
payments (
    id, user_id,
    order_id        TEXT UNIQUE,   -- lendings-XXXXXXXXXXXX
    invoice_id      TEXT,          -- ID от kaspi-pos
    amount          INTEGER,       -- в тенге
    tokens          INTEGER,       -- сколько кредитов зачислить
    catalog_item_id TEXT,          -- ID товара в Kaspi каталоге
    status          TEXT,          -- pending / paid / payment.failed / payment.expired
    created/updated TEXT
)

-- Сессии
sessions (
    id      TEXT PRIMARY KEY,      -- uuid4 hex
    user_id INTEGER,
    expires TEXT                   -- +1 год от создания
)
```

---

## AI-пайплайн

### 1. Онбординг (чат до генерации)

**Эндпоинт:** `POST /chat`

AI (`CHAT_SYSTEM`) собирает 4 поля через диалог:
- `name` — имя и профессия
- `services` — услуги с ценами
- `city` — город + контакт (WhatsApp/Telegram)
- `vibe` — стиль или URL референс-сайта

Каждый ответ AI — валидный JSON:
```json
{
  "reply": "Понял! А услуги с ценами есть?",
  "ready": false,
  "collected": {"name": "Айгуль, маникюр", "services": null, "city": null, "vibe": null},
  "_usage": {"inp": 310, "out": 45, "cr": 280}
}
```

Когда `ready: true` — сервер сразу запускает `_ai_generate()`.

### 2. Дизайн-бриф с URL

Если `vibe` — это URL: `_fetch_url()` скачивает HTML/CSS сайта, вытаскивает токены:
- CSS-переменные (`--primary-color: #...`)
- Цвета, шрифты, `border-radius`, `box-shadow`, Google Fonts URLs

Бриф передаётся в промпт генерации — Claude точно воспроизводит стилистику референса.

### 3. Генерация HTML

**Функция:** `_ai_generate(data)`

- Модель: `claude-haiku-4-5-20251001-v1:0` через Bedrock
- `max_tokens: 8192`
- `SYSTEM_PROMPT` кешируется (`cache_control: ephemeral`) — экономия ~90% на повторных вызовах
- Результат: полный `<!DOCTYPE html>` сайт, сохраняется в `generated_sites/<slug>.html`

### 4. Редактирование сайта

**Эндпоинт:** `POST /site/{slug}/edit`

Двухшаговый процесс:
1. `_ai_edit_chat()` — уточняет запрос (может вернуть `needs_photos: true` если нужны фото)
2. Когда `ready: true` — `_ai_generate()` с `edit_request` + `prev_html_full` (патчит существующий HTML)

---

## Платёжная система

**Через kaspi-pos** на сервере `92.38.49.113:4001` (astana-gb-project).

### Пакеты

| catalog_item_id        | Тип     | Цена   | Слоты | Кредиты |
|------------------------|---------|--------|-------|---------|
| 17785986704184106      | slot    | 5 000₸ | +1    | +1 000  |
| 17785986704186047      | credits | 990₸   | 0     | +500    |
| 17785986704193557      | credits | 2 490₸ | 0     | +1 500  |

**Слот** — право создать ещё один сайт + кредиты на правки.  
**Кредиты** — только кредиты, без нового слота.

### Поток оплаты

1. `POST /payment/create` — создаёт инвойс в kaspi-pos, сохраняет `pending` в payments
2. Клиент подтверждает в приложении Kaspi на телефоне
3. Kaspi-pos посылает webhook `POST /payment/webhook` с HMAC-подписью (`X-Apipay-Signature: sha256=<hex>`)
4. При `payment.success`:
   - `type=slot` → `db.add_site_slot()` — +1 слот + кредиты
   - `type=credits` → `db.add_tokens()` — только кредиты
5. `GET /payment/status/{order_id}` — полинг статуса с фронта

**KASPI_WH_SECRET:** `b8daafada57acef22720443606cacb441bc4bd0228b6374f627a8b75d474edf0`  
**KASPI_API_KEY:** `lendings-kaspi-key`

---

## Токены и стоимость

```python
PRICE_INPUT  = $1.00 / 1M tokens
PRICE_OUTPUT = $5.00 / 1M tokens

# Конвертация: 1K claude tokens = 1 наш кредит
our_credits = max(1, round((input_tokens + output_tokens) / 1_000))

# Кеш-скидки:
# cache_read:   10% от input price
# cache_create: 125% от input price
```

Новый пользователь получает **0 кредитов** при регистрации — сразу редиректится на оплату (`/payment?reason=welcome`).

---

## Роуты

| Метод  | URL                          | Auth | Описание                                      |
|--------|------------------------------|------|-----------------------------------------------|
| GET    | `/`                          | —    | Публичный лендинг                             |
| GET    | `/auth`                      | —    | Страница входа/регистрации                    |
| POST   | `/auth/register`             | —    | Регистрация (phone + password + name)         |
| POST   | `/auth/login`                | —    | Вход                                          |
| GET    | `/auth/google`               | —    | Старт Google OAuth (redirect to Google)       |
| GET    | `/auth/google/callback`      | —    | Callback Google OAuth                         |
| POST   | `/auth/logout`               | ✓    | Удаляет сессию, чистит cookie                 |
| GET    | `/create`                    | ✓+слот | Чат-онбординг (создать/редактировать сайт)  |
| GET    | `/dashboard`                 | ✓+слот | Список сайтов пользователя                  |
| GET    | `/profile`                   | ✓    | Профиль + лог токенов                         |
| POST   | `/profile/update`            | ✓    | Обновить имя                                  |
| GET    | `/site/{slug}`               | —    | Показать сгенерированный сайт                 |
| POST   | `/site/{slug}/edit`          | ✓    | Редактировать сайт через чат                  |
| POST   | `/site/{slug}/delete`        | ✓    | Удалить сайт и HTML-файл                      |
| GET    | `/start`                     | ✓    | Начало чата (приветствие)                     |
| POST   | `/chat`                      | ✓    | Шаг онбординга / запуск генерации             |
| POST   | `/upload-photo`              | —    | Загрузка фото (→ `static/uploads/`)           |
| GET    | `/payment`                   | ✓    | Страница покупки пакета                       |
| POST   | `/payment/create`            | ✓    | Создать инвойс Kaspi                          |
| GET    | `/payment/status/{order_id}` | ✓    | Статус оплаты                                 |
| POST   | `/payment/webhook`           | HMAC | Webhook от kaspi-pos                          |
| GET    | `/admin`                     | admin| Админ-панель                                  |
| GET    | `/admin/api/stats`           | admin| Статистика (users, sites, costs, payments)    |
| GET    | `/admin/api/users`           | admin| Список пользователей                          |
| GET    | `/admin/api/user/{uid}`      | admin| Детали пользователя + сайты + лог             |
| POST   | `/admin/api/user/{uid}/add-tokens` | admin | Начислить токены вручную             |

**Admin phone:** `77064177628`

---

## Сервер и деплой

### Параметры сервера

| Параметр     | Значение                                       |
|-------------|------------------------------------------------|
| IP          | `92.38.48.227`                                 |
| Домен       | `dum-e.com`                                    |
| SSH         | `ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227` |
| App dir     | `/opt/lendings/`                               |
| Venv        | `/opt/lendings/venv/`                          |
| Port        | `127.0.0.1:8002` (за nginx)                    |
| Systemd     | `lendings.service`                             |
| Nginx conf  | `/etc/nginx/sites-available/dum-e.conf`        |
| DB          | `/opt/lendings/lendings.db`                    |
| Uploads     | `/opt/lendings/static/uploads/`                |
| Sites       | `/opt/lendings/generated_sites/`               |

### Переменные окружения (в systemd unit)

```ini
Environment=AWS_BEARER_TOKEN_BEDROCK=<base64-token>   # истекает, нужно обновлять
Environment=AWS_REGION=us-east-1
```

При ошибке `AuthenticationException` от Bedrock — обновить токен:
```bash
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227
sudo nano /etc/systemd/system/lendings.service
# Обновить AWS_BEARER_TOKEN_BEDROCK
sudo systemctl daemon-reload && sudo systemctl restart lendings
```

### Деплой (локалка → прод)

```bash
rsync -av \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='generated_sites/' --exclude='lendings.db' \
  --exclude='static/uploads/' --exclude='costs.json' \
  -e "ssh -i ~/.ssh/id_ed25519" \
  ~/Documents/GitHub/lendings-killer/ deploy@92.38.48.227:/opt/lendings/

ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo systemctl restart lendings"
```

### Управление сервисом

```bash
# Логи live
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo journalctl -u lendings -f"

# Статус
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo systemctl status lendings"

# Рестарт
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227 "sudo systemctl restart lendings"
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
# → http://localhost:8002
```

---

## Auth

- Cookie `sid` (httponly, samesite=lax, 1 год)
- Middleware `SessionMiddleware` вешает `request.state.user` на каждый запрос
- `_require_paid(user)` — редиректит на `/payment` если нет слотов
- Пароли — bcrypt

### Google OAuth 2.0

Опциональный вход через Google. Включается если заданы env vars:
```ini
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://dum-e.com/auth/google/callback
```
Если переменные не заданы — кнопка Google не показывается, телефон/пароль работает как обычно.

Поведение при входе:
- Новый Google пользователь → `auth_provider='google'`, `password=NULL`, `tokens=0`
- Если `google_id` уже есть → создаётся новая сессия
- Если есть локальный пользователь с таким же подтверждённым email → привязывается к нему без перезаписи пароля
- Если `google_id` и email указывают на разных пользователей → `account_conflict`

---

## Генерация slug

```python
# Кириллица → латиница → only [a-z0-9-] → max 30 chars
slug = _slugify("Айгуль, мастер маникюра")
# → "aygul"
```

Если slug занят другим пользователем — добавляется `uuid4().hex[:4]`.

---

## Загрузка фото

`POST /upload-photo`:
- Pillow ресайзит до `900×900`, конвертирует в JPEG quality=82
- Сохраняет как `/static/uploads/<uuid12>.jpg`
- Возвращает `{"url": "/static/uploads/...", "size": N}`
- URL вставляется напрямую в HTML через `<img src="...">` (не base64)

---

## Связанные проекты

- **kaspi-pos** (`92.38.49.113:4001`) — платёжный прокси, общий с astana-gb-project
- **astana-gb-project** — живёт на том же kaspi-pos, другой `external_order_id` префикс
- **NUdatingbot** — на том же VPS `92.38.48.227`, другой systemd unit и порт
