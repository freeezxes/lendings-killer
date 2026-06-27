# AGENTS.md — lendings-killer

Полная документация для AI-агентов и разработчиков: архитектура, бизнес-логика, API, деплой.

---

## Главные правила
После внедрения в продакшн необходимо проверить работу фич внедренных в проект, их работоспособность и отсутствие ошибок. Для этого необходимо создать тестовых пользователей и проверить работу фич от их имени. Если будут обнаружены ошибки, то необходимо их исправить.

Например, если внедряешь платежную систему необходимо проверить работу платежной системы от имени тестовых пользователей ровно до того момента пока не нужен будет human in loop, при случаях где невозможно сделать без человека необходимо попросить, чтобы человек это сделал.



## Что это

AI SaaS для создания сайтов-визиток. Малый бизнес (барберы, мастера маникюра, репетиторы, массажисты) отвечает на вопросы в чате — через 30–60 секунд получает готовый HTML-сайт с красивым дизайном, адаптированным под их нишу.

**Прод:** `https://dum-e.com` (временный домен, цель — `lendings.kz`)

---

## Архитектура

```
FastAPI (main.py)
    ├── SessionMiddleware  — cookie-based auth (sid), инжектит `sites_count`
    ├── /chat              — AI-онбординг + генерация сайта
    ├── /site/{slug}/edit  — AI-редактирование готового сайта
    ├── /payment/*         — Kaspi Pay (с поддержкой промокодов)
    └── /admin/*           — внутренняя админка

db.py — SQLite (синхронный, sqlite3)
    ├── Основные таблицы: users, sites, sessions, payments
    ├── Балансы: dev_credit_log, promo_credit_log
    ├── Фичи: site_versions, support_invoices, promotion_setups, analytics_events

AI: Alem.plus (Qwen 3.6)
    ├── CHAT_SYSTEM      — онбординг-диалог (JSON-ответы, ready:bool)
    ├── EDIT_CHAT_SYSTEM — диалог правок (JSON-ответы, ready:bool)
    └── SYSTEM_PROMPT    — генерация HTML
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
│   ├── dashboard.html   # Личный кабинет, баланс слотов
│   ├── profile.html     # Профиль + история балансов
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

В базе используется множество таблиц. Основные:

```sql
-- Пользователи
users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT UNIQUE,       -- только цифры, нормализован при регистрации
    password      TEXT,              -- старый формат
    password_hash TEXT,              -- bcrypt hash (NULL для Google OAuth)
    email         TEXT UNIQUE,       -- из Google профиля или при регистрации
    email_verified INTEGER DEFAULT 0,
    google_id     TEXT UNIQUE,       -- sub из Google ID token
    auth_provider TEXT DEFAULT 'local', -- 'local' или 'google'
    avatar_url    TEXT,              -- picture из Google профиля
    name          TEXT,
    tokens        INTEGER DEFAULT 0, -- legacy
    dev_credits   INTEGER DEFAULT 0, -- кредиты на разработку (правки/генерация)
    promo_credits INTEGER DEFAULT 0, -- кредиты на продвижение (AI-реклама)
    site_slots    INTEGER DEFAULT 0, -- сколько всего сайтов может создать (лимит)
    created/created_at/updated_at TEXT,
    last_login_at TEXT
)

-- Сайты
sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    slug        TEXT UNIQUE NOT NULL, -- URL-slug, транслитерирован из имени
    title       TEXT,
    data        TEXT,              -- JSON: name, services, city, vibe, photo_urls, chat_history
    html_path   TEXT,              -- путь к generated_sites/<slug>.html
    tokens_used INTEGER DEFAULT 0,
    support_paid_until TEXT,
    support_status TEXT DEFAULT 'active',
    promo_status TEXT DEFAULT 'not_configured',
    analytics_status TEXT DEFAULT 'unavailable',
    promo_setup_done INTEGER DEFAULT 0,
    chat_in/chat_out INTEGER DEFAULT 0,
    gen_in/gen_out   INTEGER DEFAULT 0,
    cache_read  INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0,
    created/updated TEXT
)

-- Сессии
sessions (
    id       TEXT PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    expires  TEXT NOT NULL
)

-- Платежи (Kaspi / Promo)
payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    order_id   TEXT UNIQUE NOT NULL,
    invoice_id TEXT,
    amount     INTEGER NOT NULL,
    tokens     INTEGER NOT NULL, -- legacy
    payment_kind TEXT DEFAULT 'legacy',
    promo_credits INTEGER DEFAULT 0,
    dev_credits INTEGER DEFAULT 0,
    site_id    INTEGER REFERENCES sites(id),
    support_invoice_id INTEGER,
    status     TEXT DEFAULT 'pending',
    created/updated TEXT
)

-- Логи кредитов
dev_credit_log (
    id, user_id, site_id, delta, reason, claude_in, claude_out, cache_read, cost_usd, balance_after, created
)
promo_credit_log (
    id, user_id, site_id, delta, reason, balance_after, created
)

-- Версии сайтов (бэкапы при редактировании)
site_versions (
    id, site_id, version_no, html, data, reason, created
)
```

---

## AI-пайплайн

### 1. Онбординг (чат до генерации)
**Эндпоинт:** `POST /chat`

AI (`CHAT_SYSTEM`) собирает 4 поля через диалог (`name`, `services`, `city`, `vibe`).
Когда `ready: true` — сервер сразу запускает `_ai_generate()`.

### 2. Дизайн-бриф с URL
Если `vibe` — это URL: `_fetch_url()` скачивает HTML/CSS сайта, вытаскивает токены (цвета, шрифты, CSS-переменные). Бриф передаётся в промпт генерации.

### 3. Генерация HTML
**Функция:** `_ai_generate(data)`
- Модель: `qwen3-6` через Alem.plus
- Результат: полный `<!DOCTYPE html>` сайт, сохраняется в `generated_sites/<slug>.html`.

### 4. Редактирование сайта
**Эндпоинт:** `POST /site/{slug}/edit`
Двухшаговый процесс:
1. `_ai_edit_chat()` — уточняет запрос.
2. Когда `ready: true` — `_ai_generate()` с `edit_request` + `prev_html_full`.
- Перед каждым успешным редактированием сохраняется копия в `site_versions`. Возможен откат версий.

---

## Платёжная система и Балансы

**Через kaspi-pos** на сервере `92.38.49.113:4001` (astana-gb-project).
Разделены `dev_credits` (для генерации и правок) и `promo_credits` (для AI-рекламы).  
Помимо покупок через Kaspi, реализована система промокодов (например, `test67`), которая позволяет обойти оплату и начислить кредиты/слоты бесплатно.

**Отображение слотов:**
В интерфейсе слоты отображаются как **доступный остаток** (`user.site_slots - user.sites_count`), а не общий лимит. Это реализовано через функцию `get_user_sites_count(uid)` и `SessionMiddleware`, которая автоматически добавляет `sites_count` в объект `request.state.user`.

**Токены и стоимость:**
1K qwen tokens = 1 dev_credit.
Кеш-скидки: cache_read (10% от input price), cache_create (125% от input price).

Новый пользователь получает **0 dev_credits** при регистрации и редиректится на оплату (`/payment?reason=welcome`).

---

## Сервер и деплой

### Параметры сервера
| Параметр     | Значение                                       |
|-------------|------------------------------------------------|
| IP          | `92.38.48.227`                                 |
| Домен       | `dum-e.com`                                    |
| SSH         | `ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227` |
| App dir     | `/opt/lendings/`                               |
| Systemd     | `lendings.service`                             |

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

---

## Тестирование UI (Playwright)

Для автоматического и надежного тестирования веб-интерфейса и бизнес-логики агентам рекомендуется использовать **Playwright**.

### Инструкция для AI-агента
1. **Виртуальное окружение**: В проекте (из-за системных ограничений) используется локальное окружение `venv_playwright`.
   Перед работой активируйте его: `source venv_playwright/bin/activate`.
2. **Написание тестов**: Пишите скрипты на Python с использованием синхронного API (`playwright.sync_api`).
3. **Запуск**: Выполняйте скрипты через терминал, например: `source venv_playwright/bin/activate && python test_playwright.py`.
4. **Пример базового скрипта**:
```python
from playwright.sync_api import sync_playwright

def test_example():
    with sync_playwright() as p:
        # headless=True для запуска без графического окна в терминале
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://localhost:8000") # Замените на нужный URL
        
        # Пример взаимодействия:
        # page.fill("input[name='email']", "test@example.com")
        # page.click("text=Отправить")
        
        print(f"Title: {page.title()}")
        browser.close()

if __name__ == "__main__":
    test_example()
```
