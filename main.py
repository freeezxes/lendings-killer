from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
import os, re, uuid, json, time, base64, hashlib, hmac, logging, secrets
from pathlib import Path
from urllib.parse import urlencode
import anthropic
import httpx
import db

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token as google_id_token
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GoogleAuthRequest = None
    google_id_token = None
    GOOGLE_AUTH_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Transliteration ──────────────────────────────────────────────────────────
_CYR_MAP = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z',
    'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}
def _slugify(text: str) -> str:
    t = ''.join(_CYR_MAP.get(c.lower(), c) for c in text)
    t = re.sub(r'[^a-zA-Z0-9]+', '-', t.lower()).strip('-')[:30]
    return t or uuid.uuid4().hex[:8]

# ── Claude via Bedrock ────────────────────────────────────────────────────────
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
PRICE_INPUT   = 1.00   # $1.00 per 1M input tokens
PRICE_OUTPUT  = 5.00   # $5.00 per 1M output tokens

ai_client = anthropic.AnthropicBedrock(
    aws_region=os.environ.get("AWS_REGION", "us-east-1"),
)

TEMPLATES_DIR = Path("templates")
GENERATED_DIR = Path("generated_sites")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PHONE = "77064177628"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_COOKIE_PATH = "/auth/google"
EMAIL_VERIFY_SECONDS = 3600
EMAIL_RESEND_COOLDOWN_SECONDS = 60
EMAIL_RESEND_RATE_LIMIT_WINDOW = 10 * 60
EMAIL_RESEND_RATE_LIMIT_MAX = 5
_EMAIL_RESEND_ATTEMPTS: dict[str, list[float]] = {}
_EMAIL_VERIFY_ATTEMPTS: dict[str, list[float]] = {}

# ── Kaspi Pay via kaspi-pos on astana-gb server ───────────────────────────────
KASPI_POS_URL    = "http://92.38.49.113:4001"
KASPI_API_KEY    = "lendings-kaspi-key"
KASPI_WH_SECRET  = "b8daafada57acef22720443606cacb441bc4bd0228b6374f627a8b75d474edf0"

# catalog item ids → token amounts
# type: "slot" = buy a site slot (5000₸ → +1 slot +1000 credits)
#        "credits" = buy extra credits only
PAYMENT_PACKAGES = [
    {"catalog_item_id": "17785986704184106", "type": "slot",    "slots": 1, "tokens": 1000, "price": 5000, "label": "1 сайт — 5 000 ₸",       "desc": "Слот + 1 000 кредитов на правки"},
    {"catalog_item_id": "17785986704186047", "type": "credits", "slots": 0, "tokens": 500,  "price": 990,  "label": "500 кредитов — 990 ₸",    "desc": "Только кредиты на правки"},
    {"catalog_item_id": "17785986704193557", "type": "credits", "slots": 0, "tokens": 1500, "price": 2490, "label": "1 500 кредитов — 2 490 ₸", "desc": "Только кредиты на правки"},
]

# ── System prompt — cached as stable prefix ───────────────────────────────────
SYSTEM_PROMPT = """Ты — эксперт по созданию красивых, живых HTML сайтов-визиток для малого бизнеса.

Тебе дадут:
1. Данные о бизнесе клиента (имя, услуги, контакты)
2. Дизайн-бриф референсного сайта (извлечённые токены: шрифты, цвета, CSS переменные, тени, скругления)

Твоя задача — сгенерировать ПОЛНЫЙ готовый HTML сайт, который выглядит профессионально и современно.

ОБЯЗАТЕЛЬНО включи (только CSS, никакого GSAP/JS для анимаций):
- CSS @keyframes анимации для hero при загрузке — fadeIn + slideUp через animation-delay
- CSS transition на карточках услуг — hover: scale(1.03), box-shadow, color
- CSS @keyframes pulse на WhatsApp кнопке
- Smooth scroll: html { scroll-behavior: smooth }
- Градиентные фоны, blur-эффекты, glassmorphism через чистый CSS
- CSS переменные для всей цветовой схемы в :root
- НЕ используй GSAP, ScrollTrigger или любой JS для анимаций
- НЕ ставь opacity:0 на видимых элементах без @keyframes которые их показывают
- Для сетки фото: display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px — НЕ обрезай фото, показывай в натуральном размере (height:auto)

Правила контента:
- Верни ТОЛЬКО чистый HTML начиная с <!DOCTYPE html> — никакого markdown, никаких ```
- ТОЧНО используй цвета, шрифты и CSS переменные из брифа — это реальные значения с референсного сайта
- Подключи указанные Google Fonts через <link> в <head>
- Имя: убери «Я», «меня зовут» — только само имя
- Услуги: красивые карточки с ценами, каждая услуга отдельно
- WhatsApp кнопка: реальная ссылка https://wa.me/НОМЕР (номер начиная с 7, без +)
- hero_text: цепляющий слоган 1-2 предложения
- Mobile-first, все элементы адаптивны
- Все тексты на русском языке
- Минимум 4 секции: hero, услуги, обо мне/преимущества, контакты"""


def _extract_design_tokens(css: str, html: str) -> dict:
    """Вытаскивает дизайн-токены из CSS: цвета, шрифты, радиусы, тени."""
    tokens = {}

    css_vars = re.findall(r'--([\w-]+)\s*:\s*([^;}{]+)', css)
    vars_dict = {k.strip(): v.strip() for k, v in css_vars}
    if vars_dict:
        tokens["css_variables"] = vars_dict

    colors = re.findall(r'#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|hsla?\([^)]+\)', css)
    unique_colors = list(dict.fromkeys(c for c in colors if len(c) > 3))[:15]
    if unique_colors:
        tokens["colors"] = unique_colors

    fonts = re.findall(r"font-family\s*:\s*['\"]?([A-Za-z][A-Za-z0-9 ]+)['\"]?", css)
    gfonts = re.findall(r'fonts\.googleapis\.com/css2?\?family=([^&"\']+)', css + html)
    font_names = list(dict.fromkeys(
        [f.strip() for f in fonts if len(f.strip()) > 2][:6] +
        [re.sub(r'[+:].*', '', g).replace('+', ' ') for g in gfonts]
    ))
    if font_names:
        tokens["fonts"] = font_names[:5]

    radii = re.findall(r'border-radius\s*:\s*([^;}{]+)', css)
    if radii:
        tokens["border_radius"] = list(dict.fromkeys(r.strip() for r in radii))[:4]

    shadows = re.findall(r'box-shadow\s*:\s*([^;}{]+)', css)
    if shadows:
        tokens["shadows"] = list(dict.fromkeys(s.strip() for s in shadows))[:3]

    bgs = re.findall(r'background(?:-color)?\s*:\s*([^;}{]+)', css)
    if bgs:
        tokens["backgrounds"] = list(dict.fromkeys(b.strip() for b in bgs if len(b.strip()) > 3))[:5]

    transitions = re.findall(r'transition\s*:\s*([^;}{]+)', css)
    if transitions:
        tokens["transitions"] = list(dict.fromkeys(t.strip() for t in transitions))[:3]

    gfont_urls = re.findall(r'https://fonts\.googleapis\.com/css[^"\')\s]+', css + html)
    if gfont_urls:
        tokens["google_fonts_urls"] = list(dict.fromkeys(gfont_urls))[:3]

    return tokens


def _fetch_url(url: str) -> str:
    """Скачивает CSS сайта и возвращает структурированный дизайн-бриф для Claude."""
    if not url.startswith("http"):
        url = "https://" + url

    base = re.match(r'(https?://[^/]+)', url)
    base_url = base.group(1) if base else url

    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text
    except Exception:
        return ""

    css_parts = []

    for m in re.finditer(r'<style[^>]*>(.*?)</style>', html, re.DOTALL):
        css_parts.append(m.group(1).strip())

    for href in re.findall(r'href=["\']?([^"\'> ]+\.css[^"\'> ]*)', html, re.I):
        css_url = href if href.startswith("http") else base_url + "/" + href.lstrip("/")
        try:
            cr = httpx.get(css_url, timeout=8, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0"})
            css_parts.append(cr.text)
        except Exception:
            pass

    all_css = "\n".join(css_parts)

    if len(all_css) < 200:
        return ""

    tokens = _extract_design_tokens(all_css, html)

    lines = [f"=== ДИЗАЙН-БРИФ: {url} ===\n"]

    if tokens.get("google_fonts_urls"):
        lines.append("ПОДКЛЮЧИ ЭТИ ШРИФТЫ (вставь в <head>):")
        for u in tokens["google_fonts_urls"]:
            lines.append(f'  <link href="{u}" rel="stylesheet">')

    if tokens.get("fonts"):
        lines.append(f"\nШРИФТЫ: {', '.join(tokens['fonts'])}")

    if tokens.get("css_variables"):
        lines.append("\nCSS ПЕРЕМЕННЫЕ (используй в :root):")
        for k, v in list(tokens["css_variables"].items())[:25]:
            lines.append(f"  --{k}: {v};")

    if tokens.get("colors"):
        lines.append(f"\nЦВЕТА САЙТА: {', '.join(tokens['colors'][:10])}")

    if tokens.get("backgrounds"):
        lines.append(f"\nФОНЫ: {'; '.join(tokens['backgrounds'][:3])}")

    if tokens.get("border_radius"):
        lines.append(f"\nСКРУГЛЕНИЯ: {', '.join(tokens['border_radius'])}")

    if tokens.get("shadows"):
        lines.append(f"\nТЕНИ: {'; '.join(tokens['shadows'])}")

    if tokens.get("transitions"):
        lines.append(f"\nПЕРЕХОДЫ: {'; '.join(tokens['transitions'])}")

    return "\n".join(lines)


def _is_url(text: str) -> bool:
    return bool(re.match(r'https?://|www\.', text.strip(), re.I)) or \
           bool(re.match(r'[a-zA-Z0-9-]+\.[a-zA-Z]{2,}', text.strip()))


def _ai_generate(data: dict) -> dict:
    """AI генерирует полный HTML сайт на основе данных клиента и его пожеланий по стилю."""
    ref_url = data.get("ref_url", "").strip()
    vibe    = data.get("vibe", "").strip()
    extra   = data.get("extra", "").strip()

    style_lines = []

    if ref_url and _is_url(ref_url):
        brief = _fetch_url(ref_url)
        if brief:
            style_lines.append(f"Дизайн-бриф с сайта-референса ({ref_url}):\n{brief}")
            style_lines.append("\nВАЖНО: Используй ТОЧНО цвета, шрифты и CSS переменные из брифа выше.")

    if vibe and not _is_url(vibe):
        style_lines.append(f"\nПожелание клиента по атмосфере/стилю: «{vibe}»")

    if extra and extra.lower() not in ("всё ок", "все ок", "ок", "ok", "нет", "нет пожеланий"):
        style_lines.append(f"Дополнительные пожелания: «{extra}»")

    if not style_lines:
        import random
        fallback_styles = [
            "Тёплый минимализм: кремовые тона, засечные шрифты, много воздуха",
            "Тёмный лакшери: чёрный фон, золотые акценты, элегантная типографика",
            "Свежий современный: белый фон, яркий акцент-цвет, гротескный шрифт",
            "Glassmorphism: полупрозрачные карточки, blur-эффекты, градиентный фон",
            "Editorial: крупная типографика, ассиметричный layout, контрастные блоки",
            "Pastel soft: нежные пастельные цвета, скруглённые углы, playful шрифт",
        ]
        style_lines.append(f"Стиль по умолчанию: {random.choice(fallback_styles)}")

    style_block = "\n".join(style_lines)

    photo_urls = data.get("photo_urls", [])
    if photo_urls:
        tags = "\n".join(
            f'<img src="{u}" alt="Работа" style="width:100%;height:auto;border-radius:16px;display:block;" loading="lazy">'
            for u in photo_urls
        )
        photos_block = f"\nФОТО РАБОТ — вставь в секцию портфолио именно эти теги без изменений. НЕ меняй стили, НЕ обрезай, пусть фото показываются в своём натуральном размере:\n{tags}"
    else:
        photos_block = "\nФото не добавлены — сделай красивые плейсхолдеры с эмодзи или CSS градиентами."

    # Include AI dialogue as rich context if available
    chat_history = data.get("chat_history", [])
    if chat_history:
        dialogue_lines = []
        for msg in chat_history:
            role = "Клиент" if msg["role"] == "user" else "Консультант"
            dialogue_lines.append(f"{role}: {msg['content']}")
        dialogue_block = "\n=== ДИАЛОГ С КЛИЕНТОМ (полный контекст) ===\n" + "\n".join(dialogue_lines)
    else:
        dialogue_block = ""

    edit_request = data.get("edit_request", "").strip()
    prev_html_full = data.get("prev_html_full", "").strip()

    if edit_request and prev_html_full:
        # Edit mode — patch existing HTML, don't regenerate from scratch
        user_content = f"""Вот ТЕКУЩИЙ HTML сайта клиента — измени только то, о чём просит клиент, всё остальное оставь точно как есть:

=== ТЕКУЩИЙ HTML ===
{prev_html_full}

=== ЗАПРОС КЛИЕНТА ===
«{edit_request}»

Верни ПОЛНЫЙ HTML с внесёнными изменениями. Только чистый HTML начиная с <!DOCTYPE html>, никакого markdown."""
    else:
        user_content = f"""Данные клиента:
- Имя/профессия: {data.get('name', '')}
- Услуги и цены: {data.get('services', '')}
- Город и контакт: {data.get('city', '')}
{dialogue_block}
{photos_block}

=== СТИЛЬ И ДИЗАЙН ===
{style_block}

Сгенерируй полный HTML сайт-визитку для этого клиента. Используй все детали из диалога — специализацию, нюансы бизнеса, тон общения клиента."""

    resp = ai_client.messages.create(
        model=BEDROCK_MODEL,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    usage = resp.usage
    html  = resp.content[0].text.strip()

    if html.startswith("```"):
        html = re.sub(r'^```[a-z]*\n?', '', html)
        html = re.sub(r'\n?```$', '', html)

    return {
        "html":                html,
        "input_tokens":        usage.input_tokens,
        "output_tokens":       usage.output_tokens,
        "cache_read_tokens":   getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def _calc_cost(inp: int, out: int, cr: int = 0, cc: int = 0) -> float:
    return (
        inp * PRICE_INPUT +
        out * PRICE_OUTPUT +
        cr  * PRICE_INPUT * 0.1 +
        cc  * PRICE_INPUT * 1.25
    ) / 1_000_000


def _tokens_to_ours(inp: int, out: int) -> int:
    """1K claude tokens = 1 our credit."""
    return max(1, round((inp + out) / 1_000))


# ── Cost tracking ─────────────────────────────────────────────────────────────
COSTS_FILE = Path("costs.json")

def _load_costs() -> list:
    if COSTS_FILE.exists():
        return json.loads(COSTS_FILE.read_text())
    return []

def _save_cost(entry: dict):
    rows = _load_costs()
    rows.append(entry)
    COSTS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def _ai_edit_chat(history: list, site_context: str = "") -> dict:
    """Single turn of edit dialogue — clarifies request before generating."""
    system_text = EDIT_CHAT_SYSTEM
    if site_context:
        system_text += f"\n\n=== ТЕКУЩИЙ КОНТЕНТ САЙТА ===\n{site_context}"
    resp = ai_client.messages.create(
        model=BEDROCK_MODEL,
        max_tokens=256,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=history,
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"reply": raw, "ready": False, "edit_summary": None}
    if "needs_photos" not in result:
        result["needs_photos"] = False
    return result


def _ai_chat(history: list) -> dict:
    """Run one turn of the onboarding dialogue. Returns parsed JSON + usage."""
    resp = ai_client.messages.create(
        model=BEDROCK_MODEL,
        max_tokens=512,
        system=[{
            "type": "text",
            "text": CHAT_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=history,
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"reply": raw, "ready": False, "collected": {}}
    # Attach usage so caller can accumulate
    result["_usage"] = {
        "inp": resp.usage.input_tokens,
        "out": resp.usage.output_tokens,
        "cr":  getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }
    return result


# ── Auth middleware ───────────────────────────────────────────────────────────
class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        sid = request.cookies.get("sid")
        request.state.user = db.get_session_user(sid) if sid else None
        return await call_next(request)


def _require_paid(user: dict | None) -> RedirectResponse | None:
    """Returns redirect if user has no site slots purchased."""
    if not user:
        return RedirectResponse("/auth", status_code=302)
    if not user.get("site_slots", 0):
        return RedirectResponse("/payment?reason=welcome", status_code=302)
    return None


# ── Google OAuth helpers ──────────────────────────────────────────────────────
class OAuthInvalidCode(Exception):
    pass


class OAuthServiceError(Exception):
    pass


class OAuthNoEmail(Exception):
    pass


class OAuthEmailNotVerified(Exception):
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
}


def _google_settings() -> dict:
    return {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", "").strip(),
    }


def _google_oauth_configured() -> bool:
    settings = _google_settings()
    return bool(
        GOOGLE_AUTH_AVAILABLE
        and settings["client_id"]
        and settings["client_secret"]
        and settings["redirect_uri"]
    )


def _cookie_secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "")
    app_env = os.environ.get("APP_ENV", os.environ.get("ENV", "")).lower()
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    return (
        request.url.scheme == "https"
        or proto == "https"
        or app_env in {"prod", "production"}
        or redirect_uri.startswith("https://")
    )


def _auth_context(request: Request, error: str | None = None, active_tab: str | None = None) -> dict:
    code = error or request.query_params.get("error", "")
    success = request.query_params.get("success", "")
    return {
        "error": AUTH_ERROR_MESSAGES.get(code, code) if code else None,
        "success": AUTH_SUCCESS_MESSAGES.get(success, success) if success else None,
        "active_tab": active_tab or request.query_params.get("tab", ""),
        "google_configured": _google_oauth_configured(),
    }


def _auth_error_redirect(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/auth?error={code}", status_code=302)
    response.delete_cookie(OAUTH_STATE_COOKIE, path=OAUTH_STATE_COOKIE_PATH)
    return response


def _set_session_cookie(response: RedirectResponse, request: Request, sid: str):
    response.set_cookie(
        "sid",
        sid,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=365 * 24 * 3600,
    )


def _oauth_destination(user: dict, is_new_user: bool) -> str:
    if is_new_user:
        return "/payment?reason=welcome"
    return "/dashboard" if int(user.get("tokens") or 0) > 0 else "/payment?reason=no_credits"


async def _exchange_google_code(code: str) -> str:
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

    email = db.normalize_email(payload.get("email"))
    if not email:
        raise OAuthNoEmail("Google profile has no email")

    email_verified = payload.get("email_verified")
    if email_verified not in (True, "true", "True", "1", 1):
        raise OAuthEmailNotVerified("Google email is not verified")

    google_id = (payload.get("sub") or "").strip()
    if not google_id:
        raise OAuthInvalidCode("Google profile has no subject")

    return {
        "email": email,
        "email_verified": True,
        "google_id": google_id,
        "name": (payload.get("name") or "").strip(),
        "avatar_url": (payload.get("picture") or "").strip(),
    }


# ── Email verification helpers ────────────────────────────────────────────────
class EmailServiceUnavailable(Exception):
    pass


def _email_settings() -> dict:
    return {
        "api_key": os.environ.get("RESEND_API_KEY", "").strip(),
        "from_email": os.environ.get("EMAIL_FROM", "").strip(),
        "app_base_url": os.environ.get("APP_BASE_URL", "").strip().rstrip("/"),
    }


def _email_configured() -> bool:
    settings = _email_settings()
    return bool(settings["api_key"] and settings["from_email"])


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def _verification_url(request: Request, token: str) -> str:
    base_url = _email_settings()["app_base_url"] or str(request.base_url).rstrip("/")
    return f"{base_url}/auth/verify-email?{urlencode({'token': token})}"


def _email_retry_after(user: dict | None) -> int:
    if not user or not user.get("verification_sent_at"):
        return 0
    sent_at = int(user.get("verification_sent_at") or 0)
    return max(0, EMAIL_RESEND_COOLDOWN_SECONDS - (int(time.time()) - sent_at))


def _verification_notice(request: Request, user: dict | None) -> dict:
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
    settings = _email_settings()
    if not _email_configured():
        raise EmailServiceUnavailable("Resend email is not configured")

    verify_url = _verification_url(request, token)
    html = templates.env.get_template("email_verification.html").render(
        verify_url=verify_url,
        email=user["email"],
        expires_minutes=EMAIL_VERIFY_SECONDS // 60,
    )
    payload = {
        "from": settings["from_email"],
        "to": [user["email"]],
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


async def _prepare_and_send_verification(request: Request, user: dict,
                                         rate_limit: bool = True) -> dict:
    if not user.get("email"):
        return {"ok": False, "error": "email_not_found"}
    if int(user.get("email_verified") or 0):
        return {"ok": False, "error": "email_already_verified"}
    if rate_limit and _resend_rate_limited(request, user):
        return {"ok": False, "error": "resend_rate_limited"}

    prepared = db.resend_verification_email(
        user["id"],
        cooldown_seconds=EMAIL_RESEND_COOLDOWN_SECONDS,
        expires_seconds=EMAIL_VERIFY_SECONDS,
    )
    if not prepared.get("ok"):
        return prepared

    try:
        await _send_verification_email(request, prepared["user"], prepared["token"])
    except EmailServiceUnavailable:
        logger.warning("Email verification send failed or is not configured")
        db.clear_email_verification(user["id"])
        return {"ok": False, "error": "resend_service_unavailable"}

    return prepared


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(SessionMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

UPLOADS_DIR = Path("static/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ── AI-driven onboarding chat ─────────────────────────────────────────────────
CHAT_SYSTEM = """Ты — дружелюбный консультант сервиса lendings.kz. Помогаешь мастерам и малому бизнесу создать сайт-визитку через разговор.

Твоя задача — в ходе живого диалога (3-6 сообщений) собрать всё необходимое для создания сайта:
1. Имя и ниша (кто человек, чем занимается — уточни специфику: барбер мужских стрижек? репетитор по математике? массаж спортивный или релакс?)
2. Услуги с ценами (попроси перечислить конкретные услуги и цены, если не дал)
3. Город и контакт для записи (WhatsApp/Telegram/телефон)
4. Стиль сайта — ОБЯЗАТЕЛЬНО спроси: «Как должен выглядеть сайт? Можешь описать атмосферу или скинуть ссылку на сайт с понравившимся дизайном»

Правила диалога:
- Пиши коротко, по-дружески, на «ты»
- Задавай по 1-2 вопроса за раз, не все сразу
- Если ниша понятна — задавай вопросы специфичные для неё (барберу: «стрижки только мужские?», репетитору: «какие классы/предметы?»)
- После каждого ответа кратко подтверди что понял («Понял, Астана, WhatsApp — отлично!»)
- Когда собрал имя+услуги+контакт+стиль — скажи что готов делать сайт

ВАЖНО: отвечай ТОЛЬКО валидным JSON без markdown-обёртки:
{
  "reply": "твой текст сообщения",
  "ready": false,
  "collected": {
    "name": "имя и профессия или null",
    "services": "услуги с ценами или null",
    "city": "город и контакт или null",
    "vibe": "стиль/ссылка или null"
  }
}

Когда все 4 поля собраны — ставь "ready": true и в reply напиши что-то вроде «Отлично! Всё есть — сейчас сделаю сайт ✨»"""

EDIT_CHAT_SYSTEM = """Ты — помощник по редактированию готового сайта-визитки. Тебе известен текущий контент сайта — используй эти знания при ответах.

Правила:
- Если запрос ЧЁТКИЙ — подтверди кратко и ставь ready:true
- Если запрос РАЗМЫТЫЙ — задай 1 конкретный уточняющий вопрос
- Если клиент хочет добавить ФОТО — ставь needs_photos:true, попроси загрузить через кнопку 📎 внизу
- Не задавай больше 1 вопроса за раз, пиши коротко, на «ты»
- Ты ЗНАЕШЬ что сейчас на сайте — не спрашивай то что уже есть в контексте

Примеры:
- «поменяй цвет на тёмный» → ready:true
- «добавь фото работ» → needs_photos:true, «Загрузи фото через кнопку 📎 ниже — добавлю в галерею»
- «сделай красивее» → ready:false, «Что именно: цвета, шрифты, структура?»
- «добавь раздел с отзывами» → ready:true
- «переделай полностью» → ready:false, «В каком направлении — другой стиль, другие цвета, другая структура?»

ВАЖНО: отвечай ТОЛЬКО валидным JSON:
{
  "reply": "твой ответ",
  "ready": true или false,
  "edit_summary": "краткое описание что именно менять (для передачи в генератор) или null если ready:false"
}"""


# ── Helper: require auth ──────────────────────────────────────────────────────
def _require_auth(request: Request):
    """Returns user dict if authenticated, else None."""
    return request.state.user


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html")


@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request):
    user = _require_auth(request)
    if blocked := _require_paid(user):
        return blocked
    # Check slot availability (only for new site, not edit)
    edit_slug_check = request.query_params.get("edit", "").strip()
    if not edit_slug_check:
        sites = db.get_user_sites(user["id"])
        if len(sites) >= user.get("site_slots", 0):
            return RedirectResponse("/payment?reason=no_slots", status_code=302)
    # ?edit=slug — load existing site into edit mode
    edit_slug = request.query_params.get("edit", "").strip()
    edit_slug = re.sub(r"[^a-zA-Z0-9_-]", "", edit_slug)
    edit_site = None
    if edit_slug:
        site = db.get_site_by_slug(edit_slug)
        if site and site["user_id"] == user["id"]:
            site_data = site.get("data") or {}
            edit_site = {
                "slug":    site["slug"],
                "title":   site["title"],
                "history": json.dumps(site_data.get("chat_history", []), ensure_ascii=False),
            }
    return templates.TemplateResponse(request, "index.html", {"user": user, "edit_site": edit_site})


@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    if request.state.user:
        return RedirectResponse("/create", status_code=302)
    return templates.TemplateResponse(request, "auth.html", _auth_context(request))


@app.get("/auth/google")
async def auth_google(request: Request):
    if not _google_oauth_configured():
        logger.warning("Google OAuth requested but configuration is incomplete")
        return _auth_error_redirect("google_not_configured")

    settings = _google_settings()
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings["client_id"],
        "redirect_uri": settings["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "include_granted_scopes": "true",
    }
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=10 * 60,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path=OAUTH_STATE_COOKIE_PATH,
    )
    return response


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    google_error = request.query_params.get("error")
    if google_error:
        logger.info("Google OAuth callback returned error=%s", google_error)
        return _auth_error_redirect("user_cancelled" if google_error == "access_denied" else "oauth_failed")

    expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
    received_state = request.query_params.get("state", "")
    if not expected_state or not received_state or not secrets.compare_digest(expected_state, received_state):
        logger.warning("Google OAuth state validation failed")
        return _auth_error_redirect("invalid_state")

    if not _google_oauth_configured():
        logger.warning("Google OAuth callback received but configuration is incomplete")
        return _auth_error_redirect("google_not_configured")

    code = request.query_params.get("code", "")
    if not code:
        logger.warning("Google OAuth callback missing authorization code")
        return _auth_error_redirect("invalid_code")

    try:
        id_token_value = await _exchange_google_code(code)
        profile = _verify_google_profile(id_token_value)
    except OAuthInvalidCode:
        logger.exception("Google OAuth failed during code or ID token validation")
        return _auth_error_redirect("invalid_code")
    except OAuthNoEmail:
        logger.warning("Google OAuth rejected because no email was returned")
        return _auth_error_redirect("google_no_email")
    except OAuthEmailNotVerified:
        logger.warning("Google OAuth rejected because email was not verified")
        return _auth_error_redirect("email_not_verified")
    except OAuthServiceError:
        logger.exception("Google OAuth service error")
        return _auth_error_redirect("oauth_service_error")
    except Exception:
        logger.exception("Unexpected Google OAuth callback failure")
        return _auth_error_redirect("oauth_failed")

    try:
        user_by_google = db.get_user_by_google_id(profile["google_id"])
        user_by_email = db.get_user_by_email(profile["email"])

        if user_by_google and user_by_email and user_by_google["id"] != user_by_email["id"]:
            logger.warning("Google OAuth account conflict: google_id and email map to different users")
            return _auth_error_redirect("account_conflict")

        is_new_user = False
        if user_by_google:
            user = db.link_google_to_existing_user(
                user_by_google["id"],
                profile["email"],
                profile["google_id"],
                profile["avatar_url"],
                profile["email_verified"],
            )
        elif user_by_email:
            user = db.link_google_to_existing_user(
                user_by_email["id"],
                profile["email"],
                profile["google_id"],
                profile["avatar_url"],
                profile["email_verified"],
            )
        else:
            user = db.create_google_user(
                profile["email"],
                profile["google_id"],
                profile["name"],
                profile["avatar_url"],
                profile["email_verified"],
            )
            is_new_user = True

        if not user:
            logger.warning("Google OAuth account linking or creation failed")
            return _auth_error_redirect("account_conflict")

        sid = db.create_session(user["id"])
        response = RedirectResponse(_oauth_destination(user, is_new_user), status_code=302)
        _set_session_cookie(response, request, sid)
        response.delete_cookie(OAUTH_STATE_COOKIE, path=OAUTH_STATE_COOKIE_PATH)
        return response
    except Exception:
        logger.exception("Unexpected Google OAuth account persistence failure")
        return _auth_error_redirect("oauth_failed")


@app.post("/auth/register")
async def auth_register(
    request: Request,
    phone: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
):
    # Normalise phone: keep digits only, strip leading +
    phone = re.sub(r'[^\d]', '', phone)
    email = db.normalize_email(email)
    if not phone:
        return templates.TemplateResponse(request, "auth.html",
                                          _auth_context(request, "Неверный номер телефона", "register"), status_code=400)
    if not _valid_email(email):
        return templates.TemplateResponse(request, "auth.html",
                                          _auth_context(request, "Неверный email", "register"), status_code=400)

    user = db.create_user(phone, password, name.strip(), email)
    if user is None:
        return templates.TemplateResponse(request, "auth.html",
                                          _auth_context(request, "Этот номер или email уже зарегистрирован", "register"), status_code=400)

    sid = db.create_session(user["id"])
    verification = await _prepare_and_send_verification(request, user, rate_limit=False)
    verify_param = "sent" if verification.get("ok") else "unavailable"
    # New user has 0 credits — send to payment first
    response = RedirectResponse(f"/payment?reason=welcome&verify={verify_param}", status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.post("/auth/login")
async def auth_login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
):
    phone = re.sub(r'[^\d]', '', phone)
    user = db.verify_password(phone, password)
    if not user:
        return templates.TemplateResponse(request, "auth.html",
                                          _auth_context(request, "Неверный номер или пароль", "login"), status_code=401)

    sid = db.create_session(user["id"])
    if user.get("site_slots", 0) == 0:
        dest = "/payment?reason=welcome"
    elif user["tokens"] <= 0:
        dest = "/payment?reason=no_credits"
    else:
        dest = "/dashboard"
    response = RedirectResponse(dest, status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    sid = request.cookies.get("sid")
    if sid:
        db.delete_session(sid)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("sid")
    return response


@app.post("/auth/send-email-verification")
async def auth_send_email_verification(request: Request):
    user = _require_auth(request)
    if not user:
        return _verification_json("verification_failed", status_code=401)
    result = await _prepare_and_send_verification(request, user)
    if not result.get("ok"):
        code = result.get("error", "verification_failed")
        return _verification_json(
            code,
            status_code=429 if code in {"resend_cooldown", "resend_rate_limited"} else 400,
            retry_after=int(result.get("retry_after") or 0),
        )
    return JSONResponse({
        "ok": True,
        "message": AUTH_SUCCESS_MESSAGES["verification_sent"],
        "retry_after": EMAIL_RESEND_COOLDOWN_SECONDS,
    })


@app.post("/auth/resend-email-verification")
async def auth_resend_email_verification(request: Request):
    return await auth_send_email_verification(request)


@app.get("/auth/verify-email")
async def auth_verify_email(request: Request):
    if _verify_attempt_limited(request):
        return RedirectResponse("/auth?error=invalid_token", status_code=302)

    result = db.verify_email_token(request.query_params.get("token", ""))
    if not result.get("ok"):
        code = result.get("error", "verification_failed")
        return RedirectResponse(f"/auth?error={code}", status_code=302)

    user = db.mark_email_verified(result["user"]["id"])
    if not user:
        logger.warning("Email verification failed while marking user verified")
        return RedirectResponse("/auth?error=verification_failed", status_code=302)

    sid = db.create_session(user["id"])
    if int(user.get("site_slots") or 0):
        dest = "/dashboard?email_success=email_verified"
    else:
        dest = "/payment?reason=welcome&email_success=email_verified"
    response = RedirectResponse(dest, status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = _require_auth(request)
    if blocked := _require_paid(user):
        return blocked
    sites = db.get_user_sites(user["id"])
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "sites": sites,
        "verification_notice": _verification_notice(request, user),
    })


@app.get("/site/{slug}", response_class=HTMLResponse)
async def serve_site(slug: str):
    # Sanitise slug to prevent path traversal
    slug = re.sub(r'[^a-zA-Z0-9_-]', '', slug)
    path = GENERATED_DIR / f"{slug}.html"
    if not path.exists():
        return HTMLResponse("<h1>Сайт не найден</h1>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = _require_auth(request)
    if not user or user.get("phone") != ADMIN_PHONE:
        return HTMLResponse("<h1>403 Forbidden</h1>", status_code=403)
    return templates.TemplateResponse(request, "admin.html", {"user": user})


@app.get("/admin/api/stats")
async def admin_api_stats(request: Request):
    user = _require_auth(request)
    if not user or user.get("phone") != ADMIN_PHONE:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(db.admin_stats())


@app.get("/admin/api/users")
async def admin_api_users(request: Request):
    user = _require_auth(request)
    if not user or user.get("phone") != ADMIN_PHONE:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(db.admin_users())


@app.get("/admin/api/user/{uid}")
async def admin_api_user(uid: int, request: Request):
    user = _require_auth(request)
    if not user or user.get("phone") != ADMIN_PHONE:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    detail = db.admin_user_detail(uid)
    if not detail:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


@app.post("/admin/api/user/{uid}/add-tokens")
async def admin_add_tokens(uid: int, request: Request):
    admin = _require_auth(request)
    if not admin or admin.get("phone") != ADMIN_PHONE:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    db.add_tokens(uid, amount, "admin_grant")
    updated = db.get_user_by_id(uid)
    return JSONResponse({"ok": True, "tokens": updated["tokens"]})


# ── Upload photo ──────────────────────────────────────────────────────────────

@app.post("/upload-photo")
async def upload_photo(file: UploadFile = File(...)):
    """Сохраняет фото на диск, возвращает URL — base64 НЕ используем чтобы не раздувать промпт."""
    content = await file.read()

    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(content))
        img.thumbnail((900, 900))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        content = buf.getvalue()
        ext = "jpg"
    except ImportError:
        ext = (file.filename or "photo").rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "webp"):
            ext = "jpg"

    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    (UPLOADS_DIR / filename).write_bytes(content)
    url = f"/static/uploads/{filename}"
    return JSONResponse({"url": url, "size": len(content)})


# ── Chat / site generation ────────────────────────────────────────────────────

@app.get("/start")
async def start(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    return JSONResponse({
        "message": "Привет! Расскажи о своём бизнесе — кто ты и чем занимаешься?",
        "history": [],
        "done": False,
    })


@app.post("/chat")
async def chat(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    body           = await request.json()
    message        = body.get("message", "").strip()
    history        = body.get("history", [])
    photo_urls     = body.get("photo_urls", [])
    # Accumulated chat tokens from previous turns (client sends back)
    acc_chat_in    = int(body.get("chat_in", 0))
    acc_chat_out   = int(body.get("chat_out", 0))
    acc_chat_cr    = int(body.get("chat_cr", 0))

    if not message:
        return JSONResponse({"error": "Пустое сообщение"}, status_code=400)

    history.append({"role": "user", "content": message})

    result     = _ai_chat(history)
    reply      = result.get("reply", "Продолжай, я слушаю")
    ready      = result.get("ready", False)
    collected  = result.get("collected", {})
    usage      = result.get("_usage", {})

    # Accumulate chat tokens
    acc_chat_in  += usage.get("inp", 0)
    acc_chat_out += usage.get("out", 0)
    acc_chat_cr  += usage.get("cr",  0)

    history.append({"role": "assistant", "content": reply})

    if not ready:
        return JSONResponse({
            "message":  reply,
            "history":  history,
            "done":     False,
            "chat_in":  acc_chat_in,
            "chat_out": acc_chat_out,
            "chat_cr":  acc_chat_cr,
        })

    # ── Ready to generate ──────────────────────────────────────────────────
    if user["tokens"] < 1:
        return JSONResponse({"error": "Недостаточно токенов для генерации сайта"}, status_code=402)

    vibe = collected.get("vibe") or ""
    data = {
        "name":         collected.get("name") or "Бизнес",
        "services":     collected.get("services") or "",
        "city":         collected.get("city") or "",
        "vibe":         vibe,
        "extra":        "",
        "photo_urls":   photo_urls,
        "ref_url":      vibe if _is_url(vibe) else "",
        "chat_history": history,
    }

    gen = _ai_generate(data)

    gen_in  = gen["input_tokens"]
    gen_out = gen["output_tokens"]
    gen_cr  = gen["cache_read_tokens"]
    gen_cc  = gen["cache_create_tokens"]

    # Total cost = chat turns + generation
    total_in  = acc_chat_in  + gen_in
    total_out = acc_chat_out + gen_out
    total_cr  = acc_chat_cr  + gen_cr
    cost = _calc_cost(total_in, total_out, total_cr, gen_cc)
    our_tokens = _tokens_to_ours(total_in, total_out)

    name = data["name"]
    clean_name = re.sub(r'^я\s+', '', name.lower().strip())
    slug = _slugify(clean_name.split(',')[0].strip())
    existing = db.get_site_by_slug(slug)
    if existing and existing.get("user_id") != user["id"]:
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"

    (GENERATED_DIR / f"{slug}.html").write_text(gen["html"], encoding="utf-8")

    site = db.create_site(
        user_id=user["id"],
        slug=slug,
        title=name,
        data=data,
        html_path=str(GENERATED_DIR / f"{slug}.html"),
        tokens_used=our_tokens,
        chat_in=acc_chat_in,
        chat_out=acc_chat_out,
        gen_in=gen_in,
        gen_out=gen_out,
        cache_read=total_cr,
        cost_usd=cost,
    )

    db.deduct_tokens(
        user_id=user["id"],
        amount=our_tokens,
        reason=f"site_generate:{slug}",
        site_id=site["id"] if site else None,
        claude_in=total_in,
        claude_out=total_out,
        cache_read=total_cr,
        cost_usd=cost,
    )

    _save_cost({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client": name, "slug": slug, "user_id": user["id"],
        "style": data["ref_url"] or vibe or "ai-chat",
        "chat_in": acc_chat_in, "chat_out": acc_chat_out,
        "gen_in": gen_in, "gen_out": gen_out,
        "cache_read_tokens": total_cr, "cache_create_tokens": gen_cc,
        "cost_usd": round(cost, 6), "our_tokens_spent": our_tokens,
        "model": BEDROCK_MODEL,
    })

    return JSONResponse({
        "message":      reply,
        "history":      history,
        "done":         True,
        "site_url":     f"/site/{slug}",
        "cost_usd":     round(cost, 6),
        "tokens_spent": our_tokens,
        "tokens_left":  user["tokens"] - our_tokens,
    })


# ── Site edit ─────────────────────────────────────────────────────────────────

@app.post("/site/{slug}/edit")
async def site_edit(slug: str, request: Request):
    """Edit chat + generation. First clarifies request, then generates when ready."""
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)

    body           = await request.json()
    message        = body.get("message", "").strip()
    edit_history   = body.get("edit_history", [])
    client_history = body.get("history", [])
    new_photo_urls = body.get("photo_urls", [])

    if not message:
        return JSONResponse({"error": "Пустой запрос"}, status_code=400)

    # Load site data to build context for edit chat
    data = site.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data)

    # Build plain-text context from site data so AI knows what's on the site
    site_context = "\n".join(filter(None, [
        f"Название/профессия: {data.get('name', '')}",
        f"Услуги: {data.get('services', '')}",
        f"Контакт: {data.get('city', '')}",
        f"Стиль: {data.get('vibe', '')}",
        f"Фото на сайте: {'есть (' + str(len(data.get('photo_urls', []))) + ' шт.)' if data.get('photo_urls') else 'нет'}",
    ]))

    edit_history = edit_history + [{"role": "user", "content": message}]

    result       = _ai_edit_chat(edit_history, site_context)
    reply        = result.get("reply", "Понял!")
    ready        = result.get("ready", False)
    needs_photos = result.get("needs_photos", False)
    edit_summary = result.get("edit_summary") or message

    edit_history = edit_history + [{"role": "assistant", "content": reply}]

    # Needs photos — tell client to upload, don't generate yet
    if needs_photos and not new_photo_urls:
        return JSONResponse({
            "done":         False,
            "needs_photos": True,
            "message":      reply,
            "edit_history": edit_history,
        })

    # Not ready yet — clarifying question
    if not ready:
        return JSONResponse({
            "done":         False,
            "message":      reply,
            "edit_history": edit_history,
        })

    # Ready — generate
    if user["tokens"] < 1:
        return JSONResponse({"error": "Недостаточно токенов"}, status_code=402)

    data = site.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data)

    stored_history   = data.get("chat_history", [])
    combined_history = client_history if client_history else stored_history

    # Merge new photos into existing
    if new_photo_urls:
        existing_photos = data.get("photo_urls", [])
        data["photo_urls"] = existing_photos + new_photo_urls

    prev_html = (GENERATED_DIR / f"{slug}.html").read_text(encoding="utf-8") if (GENERATED_DIR / f"{slug}.html").exists() else ""
    data["edit_request"]   = edit_summary
    data["chat_history"]   = combined_history
    data["prev_html_full"] = prev_html

    gen = _ai_generate(data)

    gen_in  = gen["input_tokens"]
    gen_out = gen["output_tokens"]
    gen_cr  = gen["cache_read_tokens"]
    gen_cc  = gen["cache_create_tokens"]
    cost       = _calc_cost(gen_in, gen_out, gen_cr, gen_cc)
    our_tokens = _tokens_to_ours(gen_in, gen_out)

    (GENERATED_DIR / f"{slug}.html").write_text(gen["html"], encoding="utf-8")

    data_to_save = {**data, "chat_history": combined_history}
    db.update_site_data(site["id"], data_to_save)
    db.update_site_html(site["id"], str(GENERATED_DIR / f"{slug}.html"), our_tokens)
    db.deduct_tokens(
        user_id=user["id"], amount=our_tokens,
        reason=f"site_edit:{slug}",
        site_id=site["id"],
        claude_in=gen_in, claude_out=gen_out,
        cache_read=gen_cr, cost_usd=cost,
    )

    return JSONResponse({
        "done":         True,
        "ok":           True,
        "message":      reply,
        "site_url":     f"/site/{slug}",
        "edit_history": edit_history,
        "tokens_spent": our_tokens,
        "tokens_left":  user["tokens"] - our_tokens,
    })


# ── Payment routes ────────────────────────────────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    log = db.get_token_log(user["id"])
    sites = db.get_user_sites(user["id"])
    return templates.TemplateResponse(request, "profile.html", {
        "user": user,
        "log": log,
        "sites_count": len(sites),
        "verification_notice": _verification_notice(request, user),
    })


@app.post("/profile/update")
async def profile_update(request: Request, name: str = Form(...), email: str = Form("")):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    db.update_user_name(user["id"], name)

    new_email = db.normalize_email(email)
    current_email = db.normalize_email(user.get("email"))
    if new_email and new_email != current_email:
        if not _valid_email(new_email):
            return RedirectResponse("/profile?email_error=invalid_email", status_code=302)
        updated = db.update_user_email_for_verification(user["id"], new_email)
        if not updated:
            return RedirectResponse("/profile?email_error=account_conflict", status_code=302)
        result = await _prepare_and_send_verification(request, updated, rate_limit=False)
        if result.get("ok"):
            return RedirectResponse("/profile?email_success=verification_sent", status_code=302)
        return RedirectResponse(f"/profile?email_error={result.get('error', 'verification_failed')}", status_code=302)

    return RedirectResponse("/profile", status_code=302)


@app.post("/site/{slug}/delete")
async def site_delete(slug: str, request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return JSONResponse({"error": "Не найдено"}, status_code=404)
    db.delete_site(site["id"], user["id"])
    # Remove generated HTML file
    html_file = GENERATED_DIR / f"{slug}.html"
    if html_file.exists():
        html_file.unlink()
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/payment", response_class=HTMLResponse)
async def payment_page(request: Request):
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    reason = request.query_params.get("reason", "")
    sites  = db.get_user_sites(user["id"])
    slot_pkg    = next(p for p in PAYMENT_PACKAGES if p["type"] == "slot")
    credit_pkgs = [p for p in PAYMENT_PACKAGES if p["type"] == "credits"]
    return templates.TemplateResponse(request, "payment.html", {
        "user":        user,
        "reason":      reason,
        "sites_count": len(sites),
        "slot_pkg":    slot_pkg,
        "credit_pkgs": credit_pkgs,
        "verification_notice": _verification_notice(request, user),
    })


@app.post("/payment/create")
async def payment_create(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    body = await request.json()
    catalog_item_id = body.get("catalog_item_id")
    phone = body.get("phone", "").strip()

    pkg = next((p for p in PAYMENT_PACKAGES if p["catalog_item_id"] == catalog_item_id), None)
    if not pkg:
        return JSONResponse({"error": "Неверный пакет"}, status_code=400)

    phone_clean = re.sub(r"[^\d]", "", phone)
    if len(phone_clean) < 10:
        return JSONResponse({"error": "Введите номер телефона Kaspi"}, status_code=400)

    order_id = uuid.uuid4().hex[:12].upper()

    try:
        resp = httpx.post(
            f"{KASPI_POS_URL}/api/v1/invoices",
            headers={"X-API-Key": KASPI_API_KEY, "Content-Type": "application/json"},
            json={
                "phone_number": phone_clean,
                "external_order_id": f"lendings-{order_id}",
                "webhook_url": "https://dum-e.com/payment/webhook",
                "description": f"lendings.kz {pkg['label']}",
                "cart_items": [{"catalog_item_id": catalog_item_id, "count": 1}],
            },
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return JSONResponse({"error": f"Ошибка платежного шлюза: {e}"}, status_code=502)

    if not data.get("id"):
        return JSONResponse({"error": "Kaspi не принял платёж", "detail": data}, status_code=400)

    db.create_payment(
        user_id=user["id"],
        order_id=order_id,
        invoice_id=str(data["id"]),
        amount=pkg["price"],
        tokens=pkg["tokens"],
        status="pending",
        catalog_item_id=catalog_item_id,
    )

    return JSONResponse({
        "ok": True,
        "invoice_id": data["id"],
        "order_id": order_id,
        "message": f"Запрос отправлен на номер +{phone_clean}. Откройте Kaspi и подтвердите оплату.",
    })


@app.get("/payment/status/{order_id}")
async def payment_status(order_id: str, request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    order_id = re.sub(r"[^A-Za-z0-9]", "", order_id)
    payment = db.get_payment_by_order(order_id)
    if not payment or payment["user_id"] != user["id"]:
        return JSONResponse({"error": "Не найдено"}, status_code=404)

    return JSONResponse({"status": payment["status"], "tokens": payment["tokens"]})


@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    body_bytes = await request.body()

    # Verify HMAC signature from kaspi-pos (header: X-Apipay-Signature: sha256=<hex>)
    sig = request.headers.get("X-Apipay-Signature", "")
    expected = "sha256=" + hmac.new(KASPI_WH_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    event = json.loads(body_bytes)
    ext_id = event.get("external_order_id") or ""  # lendings-XXXX

    if not ext_id.startswith("lendings-"):
        return JSONResponse({"ok": True})  # чужое событие — игнорируем

    order_id = ext_id.removeprefix("lendings-")
    payment = db.get_payment_by_order(order_id)
    if not payment or payment["status"] != "pending":
        return JSONResponse({"ok": True})

    ev_type = event.get("event")
    if ev_type == "payment.success":
        db.complete_payment(payment["id"])
        pkg = next((p for p in PAYMENT_PACKAGES if p["catalog_item_id"] == payment.get("catalog_item_id")), None)
        if pkg and pkg.get("type") == "slot":
            db.add_site_slot(payment["user_id"], payment["tokens"], f"slot_purchase:{order_id}")
        else:
            db.add_tokens(payment["user_id"], payment["tokens"], f"credits_purchase:{order_id}")
    elif ev_type in ("payment.failed", "payment.expired"):
        db.fail_payment(payment["id"], ev_type)

    return JSONResponse({"ok": True})
