from fastapi import FastAPI, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
import os, re, uuid, json, time, base64, hashlib, hmac, logging, secrets
from pathlib import Path
from core.config import settings
from urllib.parse import urlencode
from api.routers import api_router
import httpx
import db
import auth_services
import services
from domain import (
    CAMPAIGN_MIN_CREDITS,
    CAMPAIGN_MIN_DURATION_HOURS,
    DraftValidationError,
    PROMO_CREDIT_TENGE,
    PROMO_MIN_PURCHASE,
    PROMO_SETUP_COST,
)

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token as google_id_token
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GoogleAuthRequest = None
    google_id_token = None
    GOOGLE_AUTH_AVAILABLE = False

logger = logging.getLogger(__name__)

# transliteration
_CYR_MAP = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z',
    'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}
def _slugify(text: str) -> str:
    # slugify
    t = ''.join(_CYR_MAP.get(c.lower(), c) for c in text)
    t = re.sub(r'[^a-zA-Z0-9]+', '-', t.lower()).strip('-')[:30]
    return t or uuid.uuid4().hex[:8]

# Alem.plus AI
ALEM_API_URL = settings.alem_api_url
ALEM_API_KEY = settings.alem_api_key
OCR_API_KEY = settings.ocr_api_key
ALEM_MODEL = settings.alem_model

PRICE_INPUT   = 1.00   # $1.00 per 1M input tokens
PRICE_OUTPUT  = 5.00   # $5.00 per 1M output tokens

async def _run_ocr(base64_image: str) -> str:
    headers = {
        "Authorization": f"Bearer {OCR_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-ocr",
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    },
                    {
                        "type": "text",
                        "text": "Extract all text from this image."
                    }
                ]
            }
        ]
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(ALEM_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.error(f"OCR Error: {e}")
            return ""

TEMPLATES_DIR = Path("templates")
GENERATED_DIR = Path("generated_sites")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PHONE = settings.admin_phone
# Separate Admin Auth Constants
ADMIN_SESSION_COOKIE = "admin_sid2"
ADMIN_CSRF_COOKIE = "admin_csrf2"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_COOKIE_PATH = "/auth/google"
AUTH_CSRF_COOKIE = auth_services.AUTH_CSRF_COOKIE
AUTH_MAX_BODY_BYTES = 16 * 1024
AUTH_FORM_POST_PATHS = {
    "/auth/register",
    "/auth/login",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/profile/update",
    "/admin/login",
    "/admin/register",
    "/admin/logout",
}
EMAIL_VERIFY_SECONDS = 3600
EMAIL_RESEND_COOLDOWN_SECONDS = 60
EMAIL_RESEND_RATE_LIMIT_WINDOW = 10 * 60
EMAIL_RESEND_RATE_LIMIT_MAX = 5
_EMAIL_RESEND_ATTEMPTS: dict[str, list[float]] = {}
_EMAIL_VERIFY_ATTEMPTS: dict[str, list[float]] = {}

def _api_error(message: str, status_code: int = 400, code: str = "bad_request") -> JSONResponse:
    # consistent api error response
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status_code,
    )

async def _json_body(request: Request) -> dict:
    # safe json body
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}

# kaspi pay via kaspi-pos
KASPI_POS_URL    = settings.kaspi_pos_url
KASPI_API_KEY    = settings.kaspi_api_key
KASPI_WH_SECRET  = settings.kaspi_wh_secret

# catalog item ids to token amounts
# type slot = buy a site slot
# type credits = buy extra credits only
PAYMENT_PACKAGES = [
    {"catalog_item_id": "17785986704184106", "type": "slot",    "slots": 1, "tokens": 1000, "price": 5000, "label": "1 сайт — 5 000 ₸",        "desc": "Сайт + 1 000 кредитов разработки + первый месяц поддержки"},
    {"catalog_item_id": "1783007771095454",  "type": "credits", "slots": 0, "tokens": 200,  "price": 1500, "label": "200 кредитов — 1 500 ₸",  "desc": "Кредиты разработки для AI-правок"},
    {"catalog_item_id": "17830077710963105", "type": "credits", "slots": 0, "tokens": 500,  "price": 3000, "label": "500 кредитов — 3 000 ₸",  "desc": "Кредиты разработки для AI-правок"},
    {"catalog_item_id": "17830077710967368", "type": "credits", "slots": 0, "tokens": 1000, "price": 5000, "label": "1 000 кредитов — 5 000 ₸", "desc": "Кредиты разработки для AI-правок"},
]

# system prompt cached as stable prefix
SYSTEM_PROMPT = """Ты — топовый веб-дизайнер и frontend-разработчик. Твоя задача — создать ПРЕМИАЛЬНЫЙ, невероятно стильный и живой HTML сайт-визитку для малого бизнеса, который вызывает мгновенный "WOW" эффект.

Тебе дадут:
1. Данные о бизнесе клиента (имя, услуги, цены, контакты)
2. Дизайн-бриф референсного сайта (цвета, шрифты, CSS переменные, тени, скругления)

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ВЕРСТКИ (СТРОГО):
1. **Премиальная эстетика**: 
   - Используй мягкий Glassmorphism: `background: rgba(255, 255, 255, 0.05); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1);`
   - Избегай чистых базовых цветов (red, blue). Используй глубокие градиенты (напр., `linear-gradient(135deg, #1e1e24, #2b2b36)` или акцентные неоновые HSL цвета).
   - Огромные, дышащие отступы (padding: 4rem 2rem).
   - Идеальная иерархия шрифтов: огромные заголовки (font-size: 3rem+), насыщенность (font-weight: 800), межбуквенное расстояние (letter-spacing: -0.03em).
2. **Анимации и Микро-взаимодействия (ТОЛЬКО CSS)**:
   - При появлении блоков (hero, карточки): `animation: fadeInUp 0.8s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; opacity: 0; transform: translateY(20px);`. Обязательно добавь `@keyframes fadeInUp`.
   - Hover-эффекты на карточках услуг: `transition: all 0.4s ease;`. При наведении: `transform: translateY(-8px) scale(1.02); box-shadow: 0 20px 40px rgba(0,0,0,0.2); border-color: var(--primary);`.
   - Кнопка WhatsApp должна пульсировать (`@keyframes pulse`) и при наведении светиться (`box-shadow: 0 0 20px var(--primary)`).
   - Плавный скролл: `html { scroll-behavior: smooth; }`.
3. **Структура**:
   - Hero-секция: Захватывающий заголовок, подзаголовок, и яркая кнопка CTA "Написать в WhatsApp".
   - Услуги: CSS-Grid сетка (`display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 2rem;`).
   - Контакты: Четкий призыв к действию.
   - Сделай адаптивно (mobile-first).
4. **Контент**:
   - Пиши продающие, уверенные тексты на РУССКОМ ЯЗЫКЕ.
   - СТРОГО ЗАПРЕЩЕНО использовать текстовые эмодзи (❌🚀🔥✨ и т.д.). Никаких эмодзи в тексте.
5. **Аналитика кнопок**: Для всех интерактивных кнопок и ссылок (<a> и <button>) ОБЯЗАТЕЛЬНО добавляй атрибут data-track="Название Кнопки". Например: data-track="WhatsApp", data-track="Записаться", data-track="Telegram". Это нужно для сбора статистики кликов!
   - НЕ выдумывай левые цены, адреса, отзывы или сертификаты, если их нет в данных.
   - Ссылка на WhatsApp должна быть: `href="https://wa.me/НОМЕР"` (цифры начиная с 7).
   - Никаких плейсхолдеров, никаких "Вставьте текст здесь".
6. **Графика и Стикеры (ИКОНКИ)**:
   - Обязательно вставь в <head> скрипт: `<script src="https://unpkg.com/@phosphor-icons/web"></script>`
   - Вместо эмодзи используй векторные иконки Phosphor Icons (стиль Duotone). Они выглядят как премиальные стикеры.
   - Используй иконки в HTML в виде тегов, например: `<i class="ph-duotone ph-sparkle"></i>`, `<i class="ph-duotone ph-phone-call"></i>`, `<i class="ph-duotone ph-check-circle"></i>`.
   - Задавай иконкам крупный размер (font-size) и цвет через CSS, чтобы они сочетались с палитрой сайта (например, `color: var(--primary)`).
7. **Формат ответа**:
   - Верни ТОЛЬКО чистый `<!DOCTYPE html>`. Никакого Markdown, никаких блоков ```html.
   - Подключи Google Fonts (Inter, Outfit или Roboto) в <head>.
   - Используй CSS переменные из брифа в `:root`.
   - ВАЖНО: Пиши ОЧЕНЬ КОМПАКТНЫЙ CSS. Максимум 150-200 строк стилей. Никаких гигантских кейфреймов, объединяй селекторы.
   - СРАЗУ ВЫВОДИ HTML. Никаких размышлений, иначе код оборвется и сайт сломается!
"""


def _payment_order_id() -> str:
    # kaspi external order suffix kept alphanumeric for status route
    return uuid.uuid4().hex[:12].upper()


def _kaspi_invoice(phone_clean: str, order_id: str, description: str,
                   catalog_item_id: str = "", amount: int | None = None) -> dict:
    # create invoice in kaspi-pos; catalog item is used when available
    payload = {
        "phone_number": phone_clean,
        "external_order_id": f"lendings-{order_id}",
        "webhook_url": "https://dum-e.com/payment/webhook",
        "description": description,
    }
    if catalog_item_id:
        payload["cart_items"] = [{"catalog_item_id": catalog_item_id, "count": 1}]
    else:
        payload["amount"] = int(amount or 0)

    resp = httpx.post(
        f"{KASPI_POS_URL}/api/v1/invoices",
        headers={"X-API-Key": KASPI_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    return resp.json()


def _inject_analytics(html: str, slug: str) -> str:
    # add lightweight click/page-view tracker to generated sites
    script = f"""
<script>
(function(){{
  if (window.__lendingsAnalytics) return;
  try {{ if (window.self !== window.top) return; }} catch(e) {{ return; }}
  window.__lendingsAnalytics = true;
  var endpoint = "/api/sites/{slug}/analytics/events";
  function eventType(el) {{
    if (!el) return "click:Действие";
    var track = el.getAttribute("data-track");
    if (track) return "click:" + track;
    
    var href = (el.getAttribute("href") || "");
    var text = (el.innerText || "").toLowerCase();
    if (/wa\\.me|whatsapp/i.test(href + " " + text)) return "click:WhatsApp";
    if (/t\\.me|telegram/i.test(href + " " + text)) return "click:Telegram";
    if (/instagram\\.com|instagram/i.test(href + " " + text)) return "click:Instagram";
    if (/^tel:/i.test(href)) return "click:Телефон";
    if (/услуг|цена|прайс|service|price/i.test(href + " " + text)) return "click:Прайс";
    return "click:Действие";
  }}
  function track(type, payload) {{
    try {{
      navigator.sendBeacon(endpoint, new Blob([JSON.stringify({{event_type:type,payload:payload||{{}}}})], {{type:"application/json"}}));
    }} catch (e) {{
      fetch(endpoint, {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{event_type:type,payload:payload||{{}}}}),keepalive:true}}).catch(function(){{}});
    }}
  }}
  track("page_view", {{path: location.pathname, referrer: document.referrer || ""}});
  document.addEventListener("click", function(e) {{
    var el = e.target && e.target.closest && e.target.closest("a,button");
    if (!el) return;
    track(eventType(el), {{text:(el.innerText||"").slice(0,120), href:el.getAttribute("href")||""}});
  }}, true);
}})();
</script>"""
    if "__lendingsAnalytics" in html:
        return html
    if "</body>" in html:
        return html.replace("</body>", script + "\n</body>", 1)
    return html + script


# cost tracking
COSTS_FILE = Path("costs.json")

def _load_costs() -> list:
    # load costs
    if COSTS_FILE.exists():
        return json.loads(COSTS_FILE.read_text())
    return []

def _save_cost(entry: dict):
    # save cost
    rows = _load_costs()
    rows.append(entry)
    COSTS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


# auth middleware
class SessionMiddleware(BaseHTTPMiddleware):
    # session middleware class
    async def dispatch(self, request: Request, call_next):
        # dispatch
        if request.method == "POST" and request.url.path in AUTH_FORM_POST_PATHS:
            content_length = request.headers.get("content-length")
            try:
                too_large = bool(content_length and int(content_length) > AUTH_MAX_BODY_BYTES)
            except ValueError:
                too_large = True
            if too_large:
                return JSONResponse({"ok": False, "error": "Unable to process request"}, status_code=413)
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                return JSONResponse({"ok": False, "error": "Unable to process request"}, status_code=415)
        sid = request.cookies.get("sid")
        request.state.user = None
        if sid:
            from core.database import AsyncSessionLocal
            from repositories.user_repo import user_repo
            from repositories.site_repo import site_repo
            from sqlalchemy import select
            from models.auth import Session
            import models.user
            from datetime import datetime as _dt
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Session, models.user.User)
                    .join(models.user.User, Session.user_id == models.user.User.id)
                    .where(Session.id == sid)
                )
                row = result.first()
                if row:
                    db_session, user = row
                    # sessions.expires is a UTC isoformat string (see db.create_session)
                    if db_session.expires > _dt.utcnow().isoformat():
                        request.state.user = user
                        sites = await site_repo.get_multi_by_user(session, user.id)
                        request.state.user.sites_count = len(sites)
        return await call_next(request)


def _require_paid(user: dict | None) -> RedirectResponse | None:
    # redirect if no site slots available
    if not user:
        return RedirectResponse("/auth", status_code=302)
    if not user.get("site_slots", 0):
        return RedirectResponse("/payment?reason=welcome", status_code=302)
    return None


def _dev_credits(user: dict | None) -> int:
    # dev credits
    if not user:
        return 0
    return int(user.get("dev_credits") if user.get("dev_credits") is not None else user.get("tokens") or 0)


# google oauth helpers
class OAuthInvalidCode(Exception):
    # o auth invalid code class
    pass


class OAuthServiceError(Exception):
    # o auth service error class
    pass


class OAuthNoEmail(Exception):
    # o auth no email class
    pass


class OAuthEmailNotVerified(Exception):
    # o auth email not verified class
    pass


AUTH_ERROR_MESSAGES = {
    "invalid_state": "Сессия Google входа устарела. Попробуйте ещё раз.",
    "oauth_failed": "Не удалось войти через Google. Попробуйте ещё раз.",
    "google_no_email": "Google не вернул email для этого аккаунта.",
    "email_not_verified": "Email в Google аккаунте не подтверждён.",
    "oauth_service_error": "Google OAuth временно недоступен. Попробуйте позже.",
    "google_not_configured": "Вход через Google пока не настроен.",
    "account_conflict": "Этот Google аккаунт конфликтует с существующим пользователем.",
    "invalid_code": "Google вернул неверный или просроченный код входа.",
    "user_cancelled": "Вход через Google отменён.",
    "invalid_token": "Ссылка подтверждения email неверна или уже использована.",
    "expired_token": "Ссылка подтверждения email истекла. Запросите новую.",
    "verification_failed": "Не удалось подтвердить email. Попробуйте ещё раз.",
    "resend_cooldown": "Письмо уже отправлено. Подождите минуту перед повторной отправкой.",
    "resend_rate_limited": "Слишком много запросов. Попробуйте позже.",
    "resend_service_unavailable": "Отправка email временно недоступна.",
    "email_already_verified": "Email уже подтверждён.",
    "email_not_found": "Добавьте email в профиль, чтобы подтвердить его.",
    "invalid_email": "Введите корректный email.",
}

AUTH_SUCCESS_MESSAGES = {
    "email_verified": "Email подтверждён. Можно продолжать работу.",
    "verification_sent": "Письмо для подтверждения отправлено.",
    "password_reset": "Пароль обновлён. Вы уже вошли в аккаунт.",
}


def _google_settings() -> dict:
    # google settings
    return {
        "client_id": settings.google_client_id.strip(),
        "client_secret": settings.google_client_secret.strip(),
        "redirect_uri": settings.google_redirect_uri.strip(),
    }


def _google_oauth_configured() -> bool:
    # google oauth configured
    settings = _google_settings()
    return bool(
        GOOGLE_AUTH_AVAILABLE
        and settings["client_id"]
        and settings["client_secret"]
        and settings["redirect_uri"]
    )


def _cookie_secure(request: Request) -> bool:
    # disable secure cookies on localhost for development
    if request.url.hostname in {"localhost", "127.0.0.1"}:
        return False

    proto = request.headers.get("x-forwarded-proto", "")
    app_env = settings.app_env.lower()
    redirect_uri = settings.google_redirect_uri.strip()
    return (
        request.url.scheme == "https"
        or proto == "https"
        or app_env in {"prod", "production"}
        or redirect_uri.startswith("https://")
    )


def _local_guest_enabled(request: Request) -> bool:
    # local guest enabled
    host = (request.url.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return True
    return settings.allow_guest_login.strip() == "1"


def _get_or_create_local_guest() -> dict:
    # get or create local guest
    email = "guest@localhost.test"
    user = db.get_user_by_email(email)
    if not user:
        user = db.create_user(
            phone="",
            password=secrets.token_urlsafe(24),
            name="Local Guest",
            email=email,
        )
    if not user:
        raise RuntimeError("Unable to create local guest user")

    with db.get_conn() as c:
        c.execute(
            """UPDATE users
               SET email_verified=1,
                   auth_provider='guest',
                   name=COALESCE(NULLIF(name,''), 'Local Guest'),
                   site_slots=MAX(COALESCE(site_slots,0), 3),
                   tokens=MAX(COALESCE(tokens,0), 3000),
                   dev_credits=MAX(COALESCE(dev_credits,0), 3000),
                   promo_credits=MAX(COALESCE(promo_credits,0), 1000),
                   updated_at=datetime('now')
               WHERE id=?""",
            (user["id"],),
        )
    return db.get_user_by_id(user["id"]) or user


def _auth_context(request: Request, error: str | None = None, active_tab: str | None = None) -> dict:
    # auth context
    code = error or request.query_params.get("error", "")
    success = request.query_params.get("success", "")
    return {
        "error": AUTH_ERROR_MESSAGES.get(code, code) if code else None,
        "success": AUTH_SUCCESS_MESSAGES.get(success, success) if success else None,
        "active_tab": active_tab or request.query_params.get("tab", ""),
        "google_configured": _google_oauth_configured(),
        "local_guest_enabled": _local_guest_enabled(request),
    }


def _auth_page_context(
    request: Request,
    error: str | None = None,
    active_tab: str | None = None,
    field: str | None = None,
    values: dict | None = None,
    success_message: str | None = None,
    reset_token: str | None = None,
    dev_reset_url: str | None = None,
) -> dict:
    # auth page context
    csrf_token = auth_services.CsrfService.generate()
    ctx = _auth_context(request, error, active_tab)
    ctx.update({
        "csrf_token": csrf_token,
        "field_error": field or "",
        "values": values or {},
        "success": success_message or ctx.get("success"),
        "reset_token": reset_token or request.query_params.get("token", ""),
        "dev_reset_url": dev_reset_url,
    })
    return ctx


def _set_auth_csrf_cookie(response, request: Request, token: str):
    # set auth csrf cookie
    response.set_cookie(
        AUTH_CSRF_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=2 * 3600,
    )


def _auth_template(
    request: Request,
    status_code: int = 200,
    **context,
):
    # auth template
    ctx = _auth_page_context(request, **context)
    response = templates.TemplateResponse(request, "auth.html", ctx, status_code=status_code)
    _set_auth_csrf_cookie(response, request, ctx["csrf_token"])
    return response


def _verify_auth_csrf(request: Request, csrf_token: str):
    # verify auth csrf
    auth_services.CsrfService.verify(csrf_token, request.cookies.get(AUTH_CSRF_COOKIE))


def _auth_error_redirect(code: str) -> RedirectResponse:
    # auth error redirect
    response = RedirectResponse(f"/auth?error={code}", status_code=302)
    response.delete_cookie(OAUTH_STATE_COOKIE, path=OAUTH_STATE_COOKIE_PATH)
    return response


def _set_session_cookie(response: RedirectResponse, request: Request, sid: str):
    # set session cookie
    response.set_cookie(
        "sid",
        sid,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=365 * 24 * 3600,
    )





def _oauth_destination(user: dict, is_new_user: bool) -> str:
    # oauth destination
    return "/dashboard"


async def _exchange_google_code(code: str) -> str:
    # exchange google code
    settings = _google_settings()
    payload = {
        "code": code,
        "client_id": settings["client_id"],
        "client_secret": settings["client_secret"],
        "redirect_uri": settings["redirect_uri"],
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json"},
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise OAuthServiceError("Invalid Google token response") from exc

    if resp.status_code >= 500:
        raise OAuthServiceError("Google token endpoint failed")
    if resp.status_code >= 400:
        if data.get("error") in {"invalid_grant", "invalid_request"}:
            raise OAuthInvalidCode("Google rejected OAuth code")
        raise OAuthServiceError("Google token endpoint rejected request")

    token = data.get("id_token")
    if not token:
        raise OAuthServiceError("Google token response did not include id_token")
    return token


def _verify_google_profile(id_token_value: str) -> dict:
    # verify google profile
    settings = _google_settings()
    if not GOOGLE_AUTH_AVAILABLE:
        raise OAuthServiceError("google-auth is not installed")

    try:
        payload = google_id_token.verify_oauth2_token(
            id_token_value,
            GoogleAuthRequest(),
            settings["client_id"],
        )
    except ValueError as exc:
        raise OAuthInvalidCode("Google ID token verification failed") from exc

    if payload.get("aud") != settings["client_id"]:
        raise OAuthInvalidCode("Google ID token audience mismatch")
    if payload.get("iss") not in GOOGLE_ISSUERS:
        raise OAuthInvalidCode("Google ID token issuer mismatch")

    try:
        email = auth_services.validate_email(payload.get("email"))
    except auth_services.AuthError:
        raise OAuthNoEmail("Google profile has no email")

    email_verified = payload.get("email_verified")
    if email_verified not in (True, "true", "True", "1", 1):
        raise OAuthEmailNotVerified("Google email is not verified")

    google_id = (payload.get("sub") or "").strip()
    if not google_id:
        raise OAuthInvalidCode("Google profile has no subject")

    try:
        name = auth_services.validate_name(payload.get("name"), required=False)
    except auth_services.AuthError:
        name = ""

    return {
        "email": email,
        "email_verified": True,
        "google_id": google_id,
        "name": name,
        "avatar_url": (payload.get("picture") or "").strip(),
    }


# email verification helpers
class EmailServiceUnavailable(Exception):
    # email service unavailable class
    pass


def _email_settings() -> dict:
    # email settings
    return {
        "api_key": settings.resend_api_key.strip(),
        "from_email": settings.email_from.strip(),
        "app_base_url": settings.app_base_url.strip().rstrip("/"),
    }


def _email_configured() -> bool:
    # email configured
    settings = _email_settings()
    return bool(settings["api_key"] and settings["from_email"])


def _valid_email(email: str) -> bool:
    # valid email
    return auth_services.is_valid_email(email)


def _verification_url(request: Request, token: str) -> str:
    # verification url
    base_url = _email_settings()["app_base_url"] or str(request.base_url).rstrip("/")
    return f"{base_url}/auth/verify-email?{urlencode({'token': token})}"


def _email_retry_after(user: dict | None) -> int:
    # email retry after
    if not user or not user.get("verification_sent_at"):
        return 0
    sent_at = int(user.get("verification_sent_at") or 0)
    return max(0, EMAIL_RESEND_COOLDOWN_SECONDS - (int(time.time()) - sent_at))


def _verification_notice(request: Request, user: dict | None) -> dict:
    # verification notice
    code = request.query_params.get("email_error", "")
    success = request.query_params.get("email_success", "")
    verify_status = request.query_params.get("verify", "")
    notice = {
        "error": AUTH_ERROR_MESSAGES.get(code, code) if code else None,
        "success": AUTH_SUCCESS_MESSAGES.get(success, success) if success else None,
        "sent": verify_status == "sent",
        "unavailable": verify_status == "unavailable",
        "retry_after": _email_retry_after(user),
    }
    if verify_status == "sent":
        notice["success"] = AUTH_SUCCESS_MESSAGES["verification_sent"]
    elif verify_status == "unavailable":
        notice["error"] = AUTH_ERROR_MESSAGES["resend_service_unavailable"]
    return notice


def _resend_rate_limited(request: Request, user: dict) -> bool:
    # resend rate limited
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    key = f"{user['id']}:{ip}"
    now = time.time()
    attempts = [
        ts for ts in _EMAIL_RESEND_ATTEMPTS.get(key, [])
        if now - ts < EMAIL_RESEND_RATE_LIMIT_WINDOW
    ]
    if len(attempts) >= EMAIL_RESEND_RATE_LIMIT_MAX:
        _EMAIL_RESEND_ATTEMPTS[key] = attempts
        return True
    attempts.append(now)
    _EMAIL_RESEND_ATTEMPTS[key] = attempts
    return False


def _verify_attempt_limited(request: Request) -> bool:
    # verify attempt limited
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    now = time.time()
    attempts = [
        ts for ts in _EMAIL_VERIFY_ATTEMPTS.get(ip, [])
        if now - ts < EMAIL_RESEND_RATE_LIMIT_WINDOW
    ]
    if len(attempts) >= 30:
        _EMAIL_VERIFY_ATTEMPTS[ip] = attempts
        return True
    attempts.append(now)
    _EMAIL_VERIFY_ATTEMPTS[ip] = attempts
    return False


def _verification_json(code: str, status_code: int = 400, retry_after: int = 0) -> JSONResponse:
    # verification json
    return JSONResponse(
        {
            "ok": False,
            "error": code,
            "message": AUTH_ERROR_MESSAGES.get(code, "Не удалось отправить письмо."),
            "retry_after": retry_after,
        },
        status_code=status_code,
    )


async def _send_verification_email(request: Request, user: dict, token: str):
    # send verification email
    settings = _email_settings()
    if not _email_configured():
        raise EmailServiceUnavailable("Resend email is not configured")

    verify_url = _verification_url(request, token)
    html = templates.env.get_template("email_verification.html").render(
        verify_url=verify_url,
        email=user.email,
        expires_minutes=EMAIL_VERIFY_SECONDS // 60,
    )
    payload = {
        "from": settings["from_email"],
        "to": [user.email],
        "subject": "Verify your email — dum-e",
        "html": html,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise EmailServiceUnavailable("Resend API request failed") from exc

    if resp.status_code >= 400:
        raise EmailServiceUnavailable(f"Resend API returned {resp.status_code}")


def _password_reset_url(request: Request, token: str) -> str:
    # password reset url
    base_url = _email_settings()["app_base_url"] or str(request.base_url).rstrip("/")
    return f"{base_url}/auth/reset?{urlencode({'token': token})}"


async def _send_password_reset_email(request: Request, reset: dict):
    # send password reset email
    settings = _email_settings()
    if not _email_configured():
        raise EmailServiceUnavailable("Resend email is not configured")

    reset_url = _password_reset_url(request, reset["token"])
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.55;color:#161620">
      <h2 style="margin:0 0 12px">Восстановление пароля lendings.kz</h2>
      <p>Нажмите кнопку ниже, чтобы задать новый пароль. Ссылка действует {reset['expires_minutes']} минут.</p>
      <p style="margin:24px 0">
        <a href="{reset_url}" style="background:#5b7cfa;color:white;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:700">Сменить пароль</a>
      </p>
      <p style="color:#667085;font-size:13px">Если вы не запрашивали восстановление, просто проигнорируйте это письмо.</p>
    </div>
    """
    payload = {
        "from": settings["from_email"],
        "to": [reset["email"]],
        "subject": "Reset your lendings.kz password",
        "html": html,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise EmailServiceUnavailable("Resend API request failed") from exc

    if resp.status_code >= 400:
        raise EmailServiceUnavailable(f"Resend API returned {resp.status_code}")


async def _prepare_and_send_verification(request: Request, user,
                                         rate_limit: bool = True) -> dict:
    # prepare and send verification
    if not user.email:
        return {"ok": False, "error": "email_not_found"}
    if user.email_verified:
        return {"ok": False, "error": "email_already_verified"}
    if rate_limit and _resend_rate_limited(request, user):
        return {"ok": False, "error": "resend_rate_limited"}

    from core.database import AsyncSessionLocal
    from repositories.user_repo import user_repo
    import time
    
    async with AsyncSessionLocal() as session:
        user = await user_repo.get(session, user.id)
        if not user:
            return {"ok": False, "error": "user_not_found"}
            
        now = int(time.time())
        sent_at = int(user.verification_sent_at or 0)
        if sent_at and now - sent_at < EMAIL_RESEND_COOLDOWN_SECONDS:
            return {
                "ok": False,
                "error": "resend_cooldown",
                "retry_after": EMAIL_RESEND_COOLDOWN_SECONDS - (now - sent_at)
            }
            
        import secrets, hashlib
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user.email_verify_token = token_hash
        user.email_verify_expires = now + EMAIL_VERIFY_SECONDS
        user.verification_sent_at = now
        session.add(user)
        await session.commit()
        await session.refresh(user)

    try:
        await _send_verification_email(request, user, token)
    except EmailServiceUnavailable:
        logger.warning("Email verification send failed or is not configured")
        async with AsyncSessionLocal() as session:
            user = await user_repo.get(session, user.id)
            user.email_verify_token = None
            user.email_verify_expires = None
            user.verification_sent_at = None
            session.add(user)
            await session.commit()
        return {"ok": False, "error": "resend_service_unavailable"}

    return {"ok": True, "token": token, "user": user}



import time
_SITE_CACHE = {}
_SITE_CACHE_TTL = 60 * 5  # 5 minutes

async def _get_cached_site_support(slug: str):
    now = time.time()
    if slug in _SITE_CACHE:
        cached_at, is_public = _SITE_CACHE[slug]
        if now - cached_at < _SITE_CACHE_TTL:
            return is_public

    from core.database import AsyncSessionLocal
    from repositories.site_repo import site_repo
    import services
    
    async with AsyncSessionLocal() as session:
        site = await site_repo.get_by_slug(session, slug)
        if not site:
            _SITE_CACHE[slug] = (now, False)
            return False
        
        site = await services.SupportService.refresh_site(session, site.id) or site
        is_pub = services.is_support_public(site.support_status)
        _SITE_CACHE[slug] = (now, is_pub)
        return is_pub

class SubdomainMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        # Отсекаем порт
        host_no_port = host.split(":")[0]
        match = re.match(r"^([a-zA-Z0-9_-]+)\.(dum-e\.com|lendings\.kz)$", host_no_port)
        
        if match:
            slug = match.group(1)
            if slug != "www":
                if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
                    return await call_next(request)
                    
                is_public = await _get_cached_site_support(slug)
                if not is_public:
                    return HTMLResponse(services.maintenance_page(), status_code=503)
                
                path = GENERATED_DIR / f"{slug}.html"
                if path.exists():
                    html = path.read_text(encoding="utf-8")
                    if "window.self !== window.top" not in html and "window.__lendingsAnalytics" in html:
                        html = html.replace("if (window.__lendingsAnalytics) return;", "if (window.__lendingsAnalytics) return;\n  try { if (window.self !== window.top) return; } catch(e) { return; }")
                    return HTMLResponse(html)
                
                return HTMLResponse("<h1>Сайт не найден</h1>", status_code=404)
                
        return await call_next(request)


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()


@app.get("/healthz")
async def healthz():
    # Lightweight liveness probe used by CI and the deploy health check.
    return {"status": "ok"}


app.include_router(api_router)
app.add_middleware(SubdomainMiddleware)
app.add_middleware(SessionMiddleware)
GENERATED_DIR.mkdir(exist_ok=True)
app.mount("/static/sites", StaticFiles(directory="generated_sites"), name="static_sites")
app.mount("/static", StaticFiles(directory="static"), name="static")
from core.templating import templates

def get_site_url(request: Request, slug: str) -> str:
    host = request.url.hostname or ""
    if host in ["dum-e.com", "lendings.kz", "www.dum-e.com", "www.lendings.kz"]:
        base_domain = host.replace("www.", "")
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        return f"{scheme}://{slug}.{base_domain}"
    return f"/site/{slug}"

templates.env.globals["get_site_url"] = get_site_url

UPLOADS_DIR = Path("static/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ── AI-driven onboarding chat ─────────────────────────────────────────────────
CHAT_SYSTEM = """Ты — дружелюбный консультант сервиса lendings.kz. Помогаешь мастерам и малому бизнесу создать сайт-визитку через разговор.

Твоя задача — в ходе живого диалога собрать всё необходимое для создания сайта, не превращая общение в жёсткую анкету:
1. Имя и ниша (кто человек, чем занимается — уточни специфику: барбер мужских стрижек? репетитор по математике? массаж спортивный или релакс?)
2. Услуги с ценами (попроси перечислить конкретные услуги и цены, если не дал)
3. Город и контакт для записи (WhatsApp/Telegram/телефон)
4. Стиль сайта или ссылка на референс. Если стиль не важен, можно принять «на твой вкус»

Правила диалога:
- Отвечай на том же языке, на котором пишет пользователь
- Пиши коротко, по-дружески, на «ты»
- Задавай по 1-2 вопроса за раз, не все сразу
- Не выдумывай цены, адрес, отзывы, гарантии, лицензии, опыт или результаты. Если данных нет — спроси или оставь пустым
- Не предлагай и не добавляй отзывы без конкретного текста отзывов от клиента
- Не обещай заявки, клиентов, продажи или медицинские/финансовые результаты
- Если ниша понятна — задавай вопросы специфичные для неё (барберу: «стрижки только мужские?», репетитору: «какие классы/предметы?»)
- Блокируй запрещённые и рискованные тематики: азартные игры, adult, финансовые пирамиды, мошенничество, запрещённые товары, политическая реклама, явно незаконные услуги, опасные медицинские обещания
- После каждого ответа кратко подтверди что понял («Понял, Астана, WhatsApp — отлично!»)
- Когда данных достаточно, в reply покажи короткий чек-бриф и спроси подтверждение, например: «Собрал: маникюр в Алматы, услуги с ценами, WhatsApp, нежный стиль. Делаю сайт?»

ВАЖНО: отвечай ТОЛЬКО валидным JSON без markdown-обёртки:
{
  "reply": "твой текст сообщения",
  "ready": false, // Обязательно ставь true ТОЛЬКО если собраны все нужные данные и ты задаешь вопрос 'Делаю сайт?'
  "collected": {
    "name": "имя и профессия или null",
    "services": "услуги с ценами или null",
    "city": "город и контакт или null",
    "vibe": "стиль/ссылка или null"
  }
}

Когда данных достаточно и чек-бриф можно показать — Обязательно ставь "ready": true."""

EDIT_CHAT_SYSTEM = """Ты — помощник по редактированию готового сайта-визитки. Тебе известен текущий контент сайта — используй эти знания при ответах.

Правила:
- Отвечай на том же языке, на котором пишет пользователь
- Если запрос ЧЁТКИЙ — подтверди кратко и ставь ready:true
- Если запрос РАЗМЫТЫЙ — задай 1 конкретный уточняющий вопрос
- Если клиент хочет добавить ФОТО или логотип, и в сообщении НЕТ [Системного уведомления] об их прикреплении — ставь needs_photos:true и попроси загрузить через кнопку 📎 внизу. Если уведомление ЕСТЬ, значит фото уже прикреплено — ставь ready:true (и needs_photos:false).
- Не задавай больше 1 вопроса за раз, пиши коротко, на «ты»
- Ты ЗНАЕШЬ что сейчас на сайте — не спрашивай то что уже есть в контексте
- Один сайт = одно направление бизнеса. Если клиент хочет превратить сайт в другой бизнес/нишу/бренд — объясни, что нужно создать отдельный сайт, ready:false
- Не добавляй отзывы, гарантии, лицензии, опыт, результаты или факты, если клиент не дал конкретное содержание
- Блокируй запрещённые и рискованные тематики: азартные игры, adult, финансовые пирамиды, мошенничество, запрещённые товары, политическая реклама, явно незаконные услуги, опасные медицинские обещания

Примеры:
- «поменяй цвет на тёмный» → ready:true
- «добавь фото работ» → needs_photos:true, «Загрузи фото через кнопку 📎 ниже — добавлю в галерею»
- «сделай красивее» → ready:false, «Что именно: цвета, шрифты, структура?»
- «добавь раздел с отзывами» → ready:false, «Пришли текст отзывов — добавлю их без выдуманных фактов»
- «переделай под аренду авто» на сайте барбера → ready:false, «Для нового направления нужно создать отдельный сайт»
- «переделай полностью» → ready:false, «В каком направлении — другой стиль, другие цвета, другая структура?»

ВАЖНО: отвечай ТОЛЬКО валидным JSON:
{
  "reply": "твой ответ",
  "ready": true или false,
  "edit_summary": "краткое описание что именно менять (для передачи в генератор) или null если ready:false"
}"""


# ── Helper: require auth ──────────────────────────────────────────────────────
def _require_auth(request: Request):
    # check if user is authenticated
    return request.state.user


async def dashboard_view(request: Request, view: str, **extra):
    # render the dashboard shell for a given view (overview/billing/create/...)
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    context = await services.build_dashboard_context(user)
    context["verification_notice"] = _verification_notice(request, context["user"])
    context["dashboard_view"] = view
    context.update(extra)
    return templates.TemplateResponse(request, "dashboard.html", context)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    # landing
    user = _require_auth(request)
    return templates.TemplateResponse(request, "landing.html", {"user": user})


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    # public product terms
    return templates.TemplateResponse(request, "terms.html", {"user": _require_auth(request)})


@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request):
    # create page
    if not _require_auth(request):
        return RedirectResponse("/auth", status_code=302)
    return RedirectResponse("/dashboard/create", status_code=302)












