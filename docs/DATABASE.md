# База данных

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
