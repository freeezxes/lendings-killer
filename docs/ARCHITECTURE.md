# Архитектура и Файловая структура

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
├── docs/                # Документация проекта
├── idea.md              # Бизнес-концепция и тарифная модель
├── costs.json           # Лог AI-расходов (не в git)
├── lendings.db          # SQLite база (не в git)
└── .gitignore
```
