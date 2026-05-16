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
- **Resend API** — transactional email для подтверждения email
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
Environment=RESEND_API_KEY=<resend-api-key>
Environment=EMAIL_FROM=noreply@dum-e.com
Environment=APP_BASE_URL=https://dum-e.com
```

Токен истекает — при ошибке Bedrock обновить значение в сервис-файле и сделать `sudo systemctl daemon-reload && sudo systemctl restart lendings`.

Google OAuth необязателен: если переменные `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` не заданы или пакет `google-auth` не установлен, обычный вход по телефону продолжает работать, а кнопка Google не показывается.

Email verification тоже graceful: если `RESEND_API_KEY` или `EMAIL_FROM` не заданы, регистрация и вход продолжают работать, но письма подтверждения не отправляются до настройки Resend.

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
pip install fastapi uvicorn anthropic httpx google-auth requests resend pillow bcrypt jinja2 python-multipart aiofiles
export AWS_BEARER_TOKEN_BEDROCK=<token>
export AWS_REGION=us-east-1
export GOOGLE_CLIENT_ID=<local-client-id>
export GOOGLE_CLIENT_SECRET=<local-client-secret>
export GOOGLE_REDIRECT_URI=http://127.0.0.1:8002/auth/google/callback
export RESEND_API_KEY=<resend-api-key>
export EMAIL_FROM=noreply@dum-e.com
export APP_BASE_URL=http://127.0.0.1:8002
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

## Email Verification через Resend

Новые local-пользователи регистрируются с email и получают письмо подтверждения. Google-пользователи с `email_verified=true` автоматически считаются подтверждёнными. Старые пользователи без email продолжают работать; подтверждение станет актуальным после добавления email в профиль.

Код отправляет письма через HTTPS endpoint Resend `POST https://api.resend.com/emails`. Пакет `resend` можно установить вместе с остальными зависимостями, но приложение использует прямой API через `httpx`, чтобы не ломаться при отсутствии SDK.

### 1. Создать Resend account

1. Откройте [resend.com](https://resend.com/).
2. Нажмите Sign up.
3. Зарегистрируйтесь на рабочий email.
4. Подтвердите email в Resend.
5. Зайдите в Dashboard.

### 2. Добавить домен `dum-e.com`

1. В Resend Dashboard откройте `Domains`.
2. Нажмите `Add Domain`.
3. Введите `dum-e.com`.
4. Сохраните домен.
5. Resend покажет DNS records для SPF/DKIM/MX. Не копируйте примеры из README вслепую: значения нужно брать именно из Resend, потому что DKIM уникален для домена.

Resend официально требует DNS records для SPF и DKIM, а также MX для bounce/complaint feedback. Документация: [Managing Domains](https://resend.com/docs/dashboard/domains/introduction).

### 3. Добавить DNS records

DNS records добавляются там, где управляется DNS домена `dum-e.com`: Cloudflare, регистратор домена, хостинг DNS или другой DNS provider.

Обычно Resend покажет:

| Record | Type | Name | Value |
|--------|------|------|-------|
| SPF | TXT | `send` | `v=spf1 include:amazonses.com ~all` |
| SPF feedback | MX | `send` | `feedback-smtp.<region>.amazonses.com` |
| DKIM | CNAME или TXT | уникальный `..._domainkey` | уникальное значение от Resend |

SPF означает Sender Policy Framework: TXT record, который говорит почтовым сервисам, что Resend/Amazon SES имеет право отправлять письма от вашего домена.

DKIM означает DomainKeys Identified Mail: криптографическая подпись писем. DNS record публикует публичный ключ, а получатель проверяет, что письмо действительно разрешено доменом.

DNS propagation: после добавления records они не всегда видны сразу. Обычно это занимает от нескольких минут до нескольких часов, иногда до 24 часов. Если Resend не видит записи сразу, подождите и нажмите `Verify DNS Records` позже.

### 4. Проверить домен в Resend

1. Вернитесь в `Domains`.
2. Откройте `dum-e.com`.
3. Нажмите `Verify DNS Records`.
4. Дождитесь статуса `Verified`.
5. Если статус не меняется, проверьте:
   - record добавлен в правильный DNS provider;
   - `Name` и `Value` совпадают с Resend;
   - DKIM records не перепутаны;
   - для MX value иногда нужен trailing dot: `feedback-smtp...amazonaws.com.`;
   - не создано две SPF TXT записи на одном и том же hostname.

### 5. Создать API key

1. В Resend Dashboard откройте `API Keys`.
2. Нажмите `Create API Key`.
3. Назовите ключ, например `lendings-production`.
4. Выберите permission для отправки email.
5. Скопируйте ключ один раз и сохраните в безопасном месте.

### 6. Env vars

Локально:

```bash
export RESEND_API_KEY=re_xxxxxxxxx
export EMAIL_FROM=noreply@dum-e.com
export APP_BASE_URL=http://127.0.0.1:8002
```

Продакшен `/etc/systemd/system/lendings.service`:

```ini
Environment=RESEND_API_KEY=re_xxxxxxxxx
Environment=EMAIL_FROM=noreply@dum-e.com
Environment=APP_BASE_URL=https://dum-e.com
```

`EMAIL_FROM` должен быть адресом на verified domain. Для понятного имени можно использовать формат `dum-e <noreply@dum-e.com>`.

### 7. Local test

```bash
source venv/bin/activate
pip install resend
export RESEND_API_KEY=re_xxxxxxxxx
export EMAIL_FROM=noreply@dum-e.com
export APP_BASE_URL=http://127.0.0.1:8002
python -m uvicorn main:app --reload --port 8002
```

Проверка:

1. Откройте `http://127.0.0.1:8002/auth`.
2. Зарегистрируйте local user с реальным email.
3. Проверьте inbox/spam.
4. Откройте ссылку `/auth/verify-email?token=...`.
5. После клика `email_verified` станет `1`; пользователь со слотом попадёт в dashboard, новый пользователь без слота останется в payment flow с success-сообщением.
6. Повторный клик по той же ссылке должен дать `invalid_token`.

### 8. Production deploy email verification

```bash
ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227
cd /opt/lendings
source venv/bin/activate
pip install resend
sudo nano /etc/systemd/system/lendings.service
```

Добавьте env vars:

```ini
Environment=RESEND_API_KEY=re_xxxxxxxxx
Environment=EMAIL_FROM=noreply@dum-e.com
Environment=APP_BASE_URL=https://dum-e.com
```

Примените:

```bash
sudo systemctl daemon-reload
sudo systemctl restart lendings
sudo journalctl -u lendings -f
```

Тест в продакшене:

1. Откройте `https://dum-e.com/auth`.
2. Зарегистрируйте тестовый аккаунт с email.
3. Убедитесь, что письмо пришло.
4. Нажмите verify link.
5. Проверьте dashboard: warning должен исчезнуть, появится verified badge.

### Troubleshooting email

| Симптом | Что проверить |
|---------|---------------|
| Письмо не приходит | `RESEND_API_KEY`, `EMAIL_FROM`, verified domain, spam folder |
| `resend_service_unavailable` | Resend env vars отсутствуют или API вернул ошибку |
| `resend_cooldown` | Повторная отправка доступна через 60 секунд |
| `resend_rate_limited` | Слишком много resend-запросов за 10 минут |
| `invalid_token` | Ссылка уже использована, повреждена или не существует |
| `expired_token` | Ссылка старше 1 часа, отправьте новую |
| Resend domain pending | DNS propagation ещё не завершился или records добавлены не туда |

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
| POST  | `/auth/send-email-verification` | Отправить письмо подтверждения текущему пользователю |
| POST  | `/auth/resend-email-verification` | Повторно отправить письмо подтверждения |
| GET   | `/auth/verify-email`          | Подтвердить email по одноразовому токену  |
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
users       — id, phone, password (bcrypt, nullable для Google), email, email_verified,
              email_verify_token, email_verify_expires, verification_sent_at, google_id,
              auth_provider, avatar_url, name, tokens, site_slots, created
sites       — id, user_id, slug, title, data (JSON), html_path, tokens_used,
              chat_in, chat_out, gen_in, gen_out, cache_read, cost_usd, created, updated
token_log   — id, user_id, site_id, delta, reason, claude_in, claude_out, cache_read, cost_usd, ts
payments    — id, user_id, order_id, invoice_id, amount, tokens, catalog_item_id, status, created
sessions    — id (hex), user_id, expires
```

Миграции выполняются при старте через `init_db()`: OAuth/email-колонки добавляются только если их нет, уникальность email/google_id обеспечивается индексами, существующие данные не удаляются и таблицы не пересоздаются.

Email verification migration добавляет nullable-поля и не форсит старых пользователей подтверждать email. Токены подтверждения хранятся как SHA-256 hash, истекают через 1 час и очищаются после успешного подтверждения.

## Rollback

Если нужно быстро откатить email verification:

1. Откатите код на предыдущий commit/tag.
2. Перезапустите сервис: `sudo systemctl restart lendings`.
3. Можно оставить новые DB columns — они nullable и не мешают старому коду.
4. Можно убрать `RESEND_API_KEY`, `EMAIL_FROM`, `APP_BASE_URL` из systemd и сделать `sudo systemctl daemon-reload && sudo systemctl restart lendings`.
5. Не удаляйте columns из SQLite вручную на продакшене: это не нужно для rollback и повышает риск потери данных.
