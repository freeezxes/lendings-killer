from fastapi import FastAPI, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
import os, re, uuid, json, time, base64, hashlib, hmac, logging, secrets
from pathlib import Path
from urllib.parse import urlencode
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
ALEM_API_URL = "https://llm.alem.ai/v1/chat/completions"
ALEM_API_KEY = os.environ.get("ALEM_API_KEY", "sk-YyCsvojyayk8wjNiEcF8tg")
ALEM_MODEL = "qwen3-6"

PRICE_INPUT   = 1.00   # $1.00 per 1M input tokens
PRICE_OUTPUT  = 5.00   # $5.00 per 1M output tokens

def _ask_llm(model: str, max_tokens: int, system_text: str, messages: list) -> dict:
    headers = {
        "Authorization": f"Bearer {ALEM_API_KEY}",
        "Content-Type": "application/json"
    }
    # OpenAI format: system prompt is the first message
    api_messages = [{"role": "system", "content": system_text}] + messages
    payload = {
        "model": model,
        "messages": api_messages,
        "max_tokens": max_tokens,
    }
    with httpx.Client(timeout=180.0) as client:
        try:
            resp = client.post(ALEM_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            # Handle reasoning models where content is empty but reasoning_content exists
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if not msg.get("content", "").strip() and msg.get("reasoning_content"):
                    msg["content"] = msg.get("reasoning_content")
            return data
        except httpx.HTTPError as e:
            msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                msg += f" | {e.response.text}"
            logger.error(f"LLM API Error: {msg}")
            fallback_content = json.dumps({
                "reply": f"Ошибка AI провайдера. Пожалуйста, обратитесь в поддержку. ({msg[:200]})", 
                "ready": False, 
                "collected": {}, 
                "needs_photos": False
            })
            return {
                "choices": [{"message": {"content": fallback_content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}
            }

TEMPLATES_DIR = Path("templates")
GENERATED_DIR = Path("generated_sites")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PHONE = "77064177628"
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
KASPI_POS_URL    = "http://92.38.49.113:4001"
KASPI_API_KEY    = "lendings-kaspi-key"
KASPI_WH_SECRET  = "b8daafada57acef22720443606cacb441bc4bd0228b6374f627a8b75d474edf0"

# catalog item ids to token amounts
# type slot = buy a site slot
# type credits = buy extra credits only
PAYMENT_PACKAGES = [
    {"catalog_item_id": "17785986704184106", "type": "slot",    "slots": 1, "tokens": 1000, "price": 5000, "label": "1 сайт — 5 000 ₸",        "desc": "Сайт + 1 000 кредитов разработки + первый месяц поддержки"},
    {"catalog_item_id": "17785986704186047", "type": "credits", "slots": 0, "tokens": 200,  "price": 1500, "label": "200 кредитов — 1 500 ₸",  "desc": "Кредиты разработки для AI-правок"},
    {"catalog_item_id": "17785986704193557", "type": "credits", "slots": 0, "tokens": 500,  "price": 3000, "label": "500 кредитов — 3 000 ₸",  "desc": "Кредиты разработки для AI-правок"},
    {"catalog_item_id": "17785986704200000", "type": "credits", "slots": 0, "tokens": 1000, "price": 5000, "label": "1 000 кредитов — 5 000 ₸", "desc": "Кредиты разработки для AI-правок"},
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
5. **Аналитика кнопок**: Для всех интерактивных кнопок и ссылок (<a> и <button>) ОБЯЗАТЕЛЬНО добавляй атрибут data-track="Название Кнопки". Например: data-track="WhatsApp", data-track="Записаться", data-track="Telegram". Это нужно для сбора статистики кликов!
   - НЕ выдумывай левые цены, адреса, отзывы или сертификаты, если их нет в данных.
   - Ссылка на WhatsApp должна быть: `href="https://wa.me/НОМЕР"` (цифры начиная с 7).
   - Никаких плейсхолдеров, никаких "Вставьте текст здесь".
5. **Формат ответа**:
   - Верни ТОЛЬКО чистый `<!DOCTYPE html>`. Никакого Markdown, никаких блоков ```html.
   - Подключи Google Fonts (Inter, Outfit или Roboto) в <head>.
   - Используй CSS переменные из брифа в `:root`.
   - ВАЖНО: Пиши ОЧЕНЬ КОМПАКТНЫЙ CSS. Максимум 150-200 строк стилей. Никаких гигантских кейфреймов, объединяй селекторы.
   - СРАЗУ ВЫВОДИ HTML. Никаких размышлений, иначе код оборвется и сайт сломается!
"""


def _extract_design_tokens(css: str, html: str) -> dict:
    # extract css design tokens
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
    # fetch and parse reference site css
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
    # is url
    return bool(re.match(r'https?://|www\.', text.strip(), re.I)) or \
           bool(re.match(r'[a-zA-Z0-9-]+\.[a-zA-Z]{2,}', text.strip()))


def _ai_generate(data: dict) -> dict:
    # generate complete html site via ai
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
            f'<img src="{u}" alt="Изображение" style="max-width:100%;height:auto;border-radius:16px;display:block;" loading="lazy">'
            for u in photo_urls
        )
        photos_block = f"\nПРИКРЕПЛЕННЫЕ ИЗОБРАЖЕНИЯ (фото работ, логотипы и т.д.):\nПользователь прикрепил эти изображения. СТРОГО ОБЯЗАТЕЛЬНО вставь эти теги <img> в подходящее место (если это логотип — в хедер, если фото работ — в портфолио). Если пользователь просит добавить логотип, УДАЛИ старый SVG-логотип или плейсхолдер и вставь ровно этот тег <img>. Не меняй сами ссылки (src)!\n{tags}"
    else:
        photos_block = "\nФото не добавлены — не создавай fake-фото, плейсхолдеры или недоделанную галерею. Сделай сайт без фотосекции, если данных не хватает."

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

Верни ПОЛНЫЙ HTML с внесёнными изменениями. Только чистый HTML начиная с <!DOCTYPE html>, никакого markdown. 
ОБЯЗАТЕЛЬНО: Пиши максимально компактно, без долгих размышлений. Твой ответ не должен превышать лимит, обязательно закрой тег </html>!"""
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

    resp = _ask_llm(
        model=ALEM_MODEL,
        max_tokens=65536,
        system_text=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    usage = resp.get("usage", {})
    html  = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

    if html.startswith("```"):
        html = re.sub(r'^```[a-z]*\n?', '', html)
        html = re.sub(r'\n?```$', '', html)

    if "</html>" not in html.lower():
        raise ValueError("Генерация прервана из-за объема (код не поместился в лимит). Пожалуйста, сделайте запрос проще.")

    # Post-process: force AI to use the real photo URLs if it hallucinated dummy src
    photo_urls = data.get("photo_urls", [])
    if photo_urls:
        for u in photo_urls:
            if u not in html:
                # Find the first img with a fake src (like logo.png or placeholder) and replace it
                def repl(m):
                    src = m.group(1)
                    if src not in photo_urls and not src.startswith("http"):
                        return m.group(0).replace(src, u)
                    return m.group(0)
                html = re.sub(r'<img\s+[^>]*src="([^"]+)"', repl, html, count=1)

    return {
        "html":                html,
        "input_tokens":        usage.get("prompt_tokens", 0),
        "output_tokens":       usage.get("completion_tokens", 0),
        "cache_read_tokens":   0,
        "cache_create_tokens": 0,
    }


def _calc_cost(inp: int, out: int, cr: int = 0, cc: int = 0) -> float:
    # calc cost
    return (
        inp * PRICE_INPUT +
        out * PRICE_OUTPUT +
        cr  * PRICE_INPUT * 0.1 +
        cc  * PRICE_INPUT * 1.25
    ) / 1_000_000


def _tokens_to_ours(inp: int, out: int) -> int:
    # calculate dev credit usage
    return max(1, round((inp + out) / 1_000))


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


def _ai_edit_chat(history: list, site_context: str = "") -> dict:
    # handle ai edit chat turn
    system_text = EDIT_CHAT_SYSTEM
    if site_context:
        system_text += f"\n\n=== ТЕКУЩИЙ КОНТЕНТ САЙТА ===\n{site_context}"
    resp = _ask_llm(
        model=ALEM_MODEL,
        max_tokens=65536,
        system_text=system_text,
        messages=history,
    )
    raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    
    import re
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        raw = match.group(1)
    elif "{" in raw and "}" in raw:
        match_brace = re.search(r"(\{.*\})", raw, re.DOTALL)
        if match_brace:
            raw = match_brace.group(1)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        if len(raw) > 400 or "thinking process" in raw.lower() or "**" in raw:
            raw = "Упс, я задумался слишком глубоко. Давайте попробуем еще раз!"
        result = {"reply": raw, "ready": False, "edit_summary": None}
    if "needs_photos" not in result:
        result["needs_photos"] = False
    return result


def _ai_chat(history: list) -> dict:
    # generate onboarding response
    resp = _ask_llm(
        model=ALEM_MODEL,
        max_tokens=65536,
        system_text=CHAT_SYSTEM,
        messages=history,
    )
    raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    
    with open("/tmp/chat_debug.log", "a") as f:
        f.write(f"RAW AI OUTPUT:\n{raw}\n\n")

    import re
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        raw = match.group(1)
    elif "{" in raw and "}" in raw:
        match_brace = re.search(r"(\{.*\})", raw, re.DOTALL)
        if match_brace:
            raw = match_brace.group(1)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        if len(raw) > 400 or "thinking process" in raw.lower() or "**" in raw:
            raw = "Упс, я задумался слишком глубоко и потерял мысль 😅 Давайте попробуем еще раз, повторите пожалуйста!"
        result = {"reply": raw, "ready": False, "collected": {}}
    
    usage = resp.get("usage", {})
    # Attach usage so caller can accumulate
    result["_usage"] = {
        "inp": usage.get("prompt_tokens", 0),
        "out": usage.get("completion_tokens", 0),
        "cr":  0,
    }
    return result


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
        request.state.user = db.get_session_user(sid) if sid else None
        if request.state.user:
            request.state.user["sites_count"] = db.get_user_sites_count(request.state.user["id"])
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
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", "").strip(),
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
    app_env = os.environ.get("APP_ENV", os.environ.get("ENV", "")).lower()
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
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
    return os.environ.get("ALLOW_GUEST_LOGIN", "").strip() == "1"


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


def _admin_setup_key() -> str:
    # optional key for creating additional admin accounts
    return os.environ.get("ADMIN_REGISTRATION_KEY", "").strip()


def _admin_registration_allowed(setup_key: str = "") -> bool:
    # allow first admin signup, then require an explicit server-side key
    required = _admin_setup_key()
    if required:
        return bool(setup_key and hmac.compare_digest(required, setup_key))
    if db.admin_count() == 0:
        return True
    return False


def _require_admin(request: Request) -> dict | None:
    # resolve separate admin session
    return db.get_admin_by_session(request.cookies.get(ADMIN_SESSION_COOKIE))


def _set_admin_session_cookie(response: RedirectResponse, request: Request, sid: str):
    # set separate admin session cookie
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        sid,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/admin",
        max_age=365 * 24 * 3600,
    )


def _set_admin_csrf_cookie(response, request: Request, token: str):
    # set csrf cookie for admin forms
    response.set_cookie(
        ADMIN_CSRF_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/admin",
        max_age=2 * 3600,
    )


def _admin_auth_template(
    request: Request,
    *,
    error: str = "",
    active_tab: str = "login",
    status_code: int = 200,
    values: dict | None = None,
    setup_key: str | None = None,
):
    # render separate admin auth page
    setup_key = request.query_params.get("setup_key", "") if setup_key is None else setup_key
    csrf_token = auth_services.CsrfService.generate()
    response = templates.TemplateResponse(
        request,
        "admin_auth.html",
        {
            "csrf_token": csrf_token,
            "error": error,
            "active_tab": active_tab,
            "values": values or {},
            "has_admins": db.admin_count() > 0,
            "registration_open": _admin_registration_allowed(setup_key),
            "setup_key": setup_key,
        },
        status_code=status_code,
    )
    _set_admin_csrf_cookie(response, request, csrf_token)
    return response


def _verify_admin_csrf(request: Request, csrf_token: str):
    # verify admin csrf
    auth_services.CsrfService.verify(csrf_token, request.cookies.get(ADMIN_CSRF_COOKIE))


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
        "api_key": os.environ.get("RESEND_API_KEY", "").strip(),
        "from_email": os.environ.get("EMAIL_FROM", "").strip(),
        "app_base_url": os.environ.get("APP_BASE_URL", "").strip().rstrip("/"),
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


async def _prepare_and_send_verification(request: Request, user: dict,
                                         rate_limit: bool = True) -> dict:
    # prepare and send verification
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


class SubdomainMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        # Отсекаем порт (например: test.dum-e.com:8000 -> test.dum-e.com)
        host_no_port = host.split(":")[0]
        
        # Разрешаем поддомены для dum-e.com и lendings.kz
        match = re.match(r"^([a-zA-Z0-9_-]+)\.(dum-e\.com|lendings\.kz)$", host_no_port)
        
        if match:
            slug = match.group(1)
            # Технические поддомены игнорируем
            if slug != "www":
                # Пропускаем API и статику к роутеру FastAPI
                if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
                    return await call_next(request)
                    
                site = db.get_site_by_slug(slug)
                if site:
                    site = services.SupportService.refresh_site(site["id"]) or site
                    if not services.is_support_public(site.get("support_status")):
                        return HTMLResponse(services.maintenance_page(), status_code=503)
                
                path = GENERATED_DIR / f"{slug}.html"
                if path.exists():
                    html = path.read_text(encoding="utf-8")
                    if "window.self !== window.top" not in html and "window.__lendingsAnalytics" in html:
                        html = html.replace("if (window.__lendingsAnalytics) return;", "if (window.__lendingsAnalytics) return;\n  try { if (window.self !== window.top) return; } catch(e) { return; }")
                    return HTMLResponse(html)
                
                # Если перешли на поддомен, но сайта нет — отдаём 404
                return HTMLResponse("<h1>Сайт не найден</h1>", status_code=404)
                
        return await call_next(request)


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(SubdomainMiddleware)
app.add_middleware(SessionMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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


@app.get("/dashboard/create", response_class=HTMLResponse)
async def dashboard_create_page(request: Request):
    # dashboard create page
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
            site = services.SupportService.refresh_site(site["id"]) or site
            if not services.is_support_operational(site.get("support_status")):
                return RedirectResponse("/dashboard?support=inactive", status_code=302)
            site_data = site.get("data") or {}
            edit_site = {
                "slug":    site["slug"],
                "title":   site["title"],
                "history": json.dumps(site_data.get("chat_history", []), ensure_ascii=False),
            }
    return await dashboard_view(
        request,
        "create",
        edit_site=edit_site,
        onboarding=services.OnboardingService.current(user["id"]) if not edit_site else None,
    )


@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    # auth page
    if request.state.user:
        return RedirectResponse("/dashboard", status_code=302)
    return _auth_template(request)


@app.api_route("/auth/guest", methods=["GET", "POST"])
async def auth_guest(request: Request):
    # auth guest
    if not _local_guest_enabled(request):
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    user = _get_or_create_local_guest()
    sid = auth_services.SessionService.create(user["id"])
    dest = request.query_params.get("next") or "/dashboard"
    if not dest.startswith("/") or dest.startswith("//"):
        dest = "/dashboard"
    response = RedirectResponse(dest, status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.get("/auth/reset", response_class=HTMLResponse)
async def auth_reset_page(request: Request):
    # auth reset page
    if request.state.user:
        return RedirectResponse("/dashboard", status_code=302)

    token = request.query_params.get("token", "")
    try:
        auth_services.PasswordResetService.validate(token)
        return _auth_template(request, active_tab="reset", reset_token=token)
    except auth_services.AuthError as exc:
        return _auth_template(request, error=exc.message, active_tab="forgot", status_code=400)


@app.get("/auth/google")
async def auth_google(request: Request):
    # auth google
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
    # auth google callback
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

        sid = auth_services.SessionService.create(user["id"])
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
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
    name: str = Form(""),
    csrf_token: str = Form(""),
):
    # auth register
    values = {
        "email": auth_services.safe_form_value(email, 254),
        "name": auth_services.safe_form_value(name, 80),
    }
    try:
        _verify_auth_csrf(request, csrf_token)
        user = auth_services.AuthService.register(
            email=email,
            password=password,
            confirm_password=confirm_password,
            name=name,
            key=auth_services.client_key(request, "register"),
        )
    except auth_services.AuthError as exc:
        return _auth_template(
            request,
            error=exc.message,
            active_tab="register",
            field=exc.field,
            values=values,
            status_code=exc.status_code,
        )

    sid = auth_services.SessionService.create(user["id"])
    verification = await _prepare_and_send_verification(request, user, rate_limit=False)
    verify_param = "sent" if verification.get("ok") else "unavailable"
    response = RedirectResponse(f"/dashboard?verify={verify_param}", status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.post("/auth/login")
async def auth_login(
    request: Request,
    email: str = Form(""),
    phone: str = Form(""),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    # auth login
    identity = email or phone
    values = {"email": auth_services.safe_form_value(identity, 254)}
    try:
        _verify_auth_csrf(request, csrf_token)
        user = auth_services.AuthService.login(
            email=identity,
            password=password,
            key=auth_services.client_key(request, "login"),
        )
    except auth_services.AuthError as exc:
        return _auth_template(
            request,
            error=exc.message,
            active_tab="login",
            field=exc.field,
            values=values,
            status_code=exc.status_code,
        )

    sid = auth_services.SessionService.create(user["id"])
    dest = "/dashboard"
    response = RedirectResponse(dest, status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.post("/auth/forgot-password")
async def auth_forgot_password(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(""),
):
    # auth forgot password
    values = {"email": auth_services.safe_form_value(email, 254)}
    try:
        _verify_auth_csrf(request, csrf_token)
        reset = auth_services.PasswordResetService.request(
            email=email,
            key=auth_services.client_key(request, "forgot"),
        )
    except auth_services.AuthError as exc:
        return _auth_template(
            request,
            error=exc.message,
            active_tab="forgot",
            field=exc.field,
            values=values,
            status_code=exc.status_code,
        )

    dev_reset_url = None
    if reset.get("sent"):
        try:
            await _send_password_reset_email(request, reset)
        except EmailServiceUnavailable:
            logger.warning("Password reset email is not configured or failed")
            if os.environ.get("APP_ENV", os.environ.get("ENV", "")).lower() not in {"prod", "production"}:
                dev_reset_url = _password_reset_url(request, reset["token"])

    return _auth_template(
        request,
        active_tab="forgot",
        success_message="Если аккаунт существует, мы отправили ссылку для восстановления.",
        values=values,
        dev_reset_url=dev_reset_url,
    )


@app.post("/auth/reset-password")
async def auth_reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    # auth reset password
    try:
        _verify_auth_csrf(request, csrf_token)
        user = auth_services.PasswordResetService.reset(
            token=token,
            password=password,
            confirm_password=confirm_password,
            key=auth_services.client_key(request, "reset"),
        )
    except auth_services.AuthError as exc:
        return _auth_template(
            request,
            error=exc.message,
            active_tab="reset" if exc.code not in {"invalid_reset_token", "expired_reset_token", "used_reset_token"} else "forgot",
            field=exc.field,
            reset_token=token,
            status_code=exc.status_code,
        )

    sid = auth_services.SessionService.create(user["id"])
    response = RedirectResponse("/dashboard?success=password_reset", status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    # auth logout
    sid = request.cookies.get("sid")
    auth_services.SessionService.delete(sid)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("sid")
    return response


@app.post("/auth/send-email-verification")
async def auth_send_email_verification(request: Request):
    # auth send email verification
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
    # auth resend email verification
    return await auth_send_email_verification(request)


@app.get("/auth/verify-email")
async def auth_verify_email(request: Request):
    # auth verify email
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

    sid = auth_services.SessionService.create(user["id"])
    dest = "/dashboard?email_success=email_verified"
    response = RedirectResponse(dest, status_code=302)
    _set_session_cookie(response, request, sid)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    # dashboard
    return await dashboard_view(request, "overview")


async def dashboard_view(request: Request, view: str, **extra):
    # dashboard view
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    context = services.build_dashboard_context(user)
    context["verification_notice"] = _verification_notice(request, context["user"])
    context["dashboard_view"] = view
    context.update(extra)
    return templates.TemplateResponse(request, "dashboard.html", {
        **context,
    })


@app.get("/dashboard/sites", response_class=HTMLResponse)
async def dashboard_sites(request: Request):
    # dashboard sites
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard/sites/{site_id}", response_class=HTMLResponse)
async def dashboard_site_workspace(site_id: int, request: Request):
    # dashboard site workspace
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    context = services.build_site_workspace_context(user, site_id)
    if not context:
        return RedirectResponse("/dashboard?missing=site", status_code=302)
    context["verification_notice"] = _verification_notice(request, context["user"])
    context["dashboard_view"] = "site"
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/dashboard/billing", response_class=HTMLResponse)
async def dashboard_billing(request: Request):
    # dashboard billing
    slot_pkg = next(p for p in PAYMENT_PACKAGES if p["type"] == "slot")
    credit_pkgs = [p for p in PAYMENT_PACKAGES if p["type"] == "credits"]
    return await dashboard_view(request, "billing", slot_pkg=slot_pkg, credit_pkgs=credit_pkgs)





@app.get("/site/{slug}", response_class=HTMLResponse)
async def serve_site(slug: str):
    # sanitise slug to prevent path traversal
    slug = re.sub(r'[^a-zA-Z0-9_-]', '', slug)
    site = db.get_site_by_slug(slug)
    if site:
        site = services.SupportService.refresh_site(site["id"]) or site
        if not services.is_support_public(site.get("support_status")):
            return HTMLResponse(services.maintenance_page(), status_code=503)
    path = GENERATED_DIR / f"{slug}.html"
    if not path.exists():
        return HTMLResponse("<h1>Сайт не найден</h1>", status_code=404)
    html = path.read_text(encoding="utf-8")
    if "window.self !== window.top" not in html and "window.__lendingsAnalytics" in html:
        html = html.replace("if (window.__lendingsAnalytics) return;", "if (window.__lendingsAnalytics) return;\n  try { if (window.self !== window.top) return; } catch(e) { return; }")
    return HTMLResponse(html)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # admin page
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/admin/login", status_code=302)
    csrf_token = auth_services.CsrfService.generate()
    response = templates.TemplateResponse(request, "admin.html", {"admin": admin, "csrf_token": csrf_token})
    _set_admin_csrf_cookie(response, request, csrf_token)
    return response


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    # separate admin login page
    if _require_admin(request):
        return RedirectResponse("/admin", status_code=302)
    return _admin_auth_template(request, active_tab="login")


@app.get("/admin/register", response_class=HTMLResponse)
async def admin_register_page(request: Request):
    # separate admin registration page
    if _require_admin(request):
        return RedirectResponse("/admin", status_code=302)
    if not _admin_registration_allowed(request.query_params.get("setup_key", "")):
        return _admin_auth_template(
            request,
            active_tab="login",
            error="Регистрация админов закрыта. Войдите или используйте server setup key.",
            status_code=403,
        )
    return _admin_auth_template(request, active_tab="register")


@app.post("/admin/login")
async def admin_login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    # separate admin login
    try:
        _verify_admin_csrf(request, csrf_token)
        email = auth_services.validate_email(email)
    except auth_services.AuthError as exc:
        return _admin_auth_template(request, active_tab="login", error=exc.message, status_code=exc.status_code)

    admin = db.verify_admin_password(email, password)
    if not admin:
        return _admin_auth_template(
            request,
            active_tab="login",
            error="Неверный email или пароль.",
            status_code=401,
            values={"email": email},
        )
    response = RedirectResponse("/admin", status_code=302)
    _set_admin_session_cookie(response, request, db.create_admin_session(admin["id"]))
    return response


@app.post("/admin/register")
async def admin_register(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    setup_key: str = Form(""),
    csrf_token: str = Form(""),
):
    # separate admin registration
    values = {"name": name, "email": email}
    try:
        _verify_admin_csrf(request, csrf_token)
        if not _admin_registration_allowed(setup_key):
            return _admin_auth_template(
                request,
                active_tab="login",
                error="Регистрация админов закрыта.",
                status_code=403,
            )
        email = auth_services.validate_email(email)
        name = auth_services.validate_name(name)
        auth_services.validate_password(password, confirm_password, email=email, name=name)
    except auth_services.AuthError as exc:
        return _admin_auth_template(
            request,
            active_tab="register",
            error=exc.message,
            status_code=exc.status_code,
            values=values,
            setup_key=setup_key,
        )

    admin = db.create_admin_user(email, password, name)
    if not admin:
        return _admin_auth_template(
            request,
            active_tab="register",
            error="Не удалось создать админа. Возможно, email уже используется.",
            status_code=400,
            values=values,
            setup_key=setup_key,
        )
    response = RedirectResponse("/admin", status_code=302)
    _set_admin_session_cookie(response, request, db.create_admin_session(admin["id"]))
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request, csrf_token: str = Form("")):
    # logout separate admin session
    try:
        _verify_admin_csrf(request, csrf_token)
    except auth_services.AuthError:
        pass
    db.delete_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@app.get("/admin/api/stats")
async def admin_api_stats(request: Request):
    # admin api stats
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(db.admin_stats())


@app.get("/admin/api/users")
async def admin_api_users(request: Request):
    # admin api users
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(db.admin_users())


@app.get("/admin/api/user/{uid}")
async def admin_api_user(uid: int, request: Request):
    # admin api user
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    detail = db.admin_user_detail(uid)
    if not detail:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


@app.post("/admin/api/user/{uid}/add-tokens")
async def admin_add_tokens(uid: int, request: Request):
    # admin add tokens
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    db.add_tokens(uid, amount, "admin_grant")
    updated = db.get_user_by_id(uid)
    return JSONResponse({"ok": True, "tokens": updated["tokens"], "dev_credits": updated["dev_credits"]})


@app.post("/admin/api/user/{uid}/add-slots")
async def admin_add_slots(uid: int, request: Request):
    # admin add site slots
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    db.add_site_slots_only(uid, amount, "admin_grant_slots")
    updated = db.get_user_by_id(uid)
    return JSONResponse({"ok": True, "site_slots": updated["site_slots"]})


# ── Upload photo ──────────────────────────────────────────────────────────────

@app.post("/upload-photo")
async def upload_photo(file: UploadFile = File(...)):
    # save photo to disk and return url
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
    # start
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    onboarding = services.OnboardingService.current(user["id"])
    return JSONResponse({
        "message": "Привет! Расскажи о своём бизнесе — кто ты и чем занимаешься?",
        "history": onboarding["session"].get("history") if onboarding.get("session") else [],
        "session": onboarding.get("session"),
        "summary": onboarding.get("summary", []),
        "progress": onboarding.get("progress", 0),
        "done": False,
    })


def _generate_site_from_session(user: dict, session: dict) -> dict:
    # generate site from session
    collected = session.get("collected") or {}
    history = session.get("history") or []
    photo_urls = session.get("photo_urls") or []
    acc_chat_in = int(session.get("chat_in") or 0)
    acc_chat_out = int(session.get("chat_out") or 0)
    acc_chat_cr = int(session.get("chat_cr") or 0)
    vibe = collected.get("vibe") or ""
    data = {
        "name": collected.get("name") or "Бизнес",
        "services": collected.get("services") or "",
        "city": collected.get("city") or "",
        "vibe": vibe,
        "extra": "",
        "photo_urls": photo_urls,
        "ref_url": vibe if _is_url(vibe) else "",
        "chat_history": history,
    }

    if not db.mark_onboarding_generating(session["id"], user["id"]):
        fresh_session = db.get_onboarding_session(session["id"], user["id"])
        if fresh_session and fresh_session.get("status") == "completed" and fresh_session.get("generated_site_id"):
            site = db.get_site_by_id(fresh_session["generated_site_id"])
            if site:
                return {
                    "ok": True,
                    "site": site,
                    "site_url": f"/site/{site['slug']}",
                    "workspace_url": f"/dashboard/sites/{site['id']}",
                    "already_done": True,
                }
        return {"ok": False, "status_code": 409, "error": "Генерация уже запущена. Обновите страницу через несколько секунд."}

    try:
        gen = _ai_generate(data)
        gen_in = gen["input_tokens"]
        gen_out = gen["output_tokens"]
        gen_cr = gen["cache_read_tokens"]
        gen_cc = gen["cache_create_tokens"]

        total_in = acc_chat_in + gen_in
        total_out = acc_chat_out + gen_out
        total_cr = acc_chat_cr + gen_cr
        cost = _calc_cost(total_in, total_out, total_cr, gen_cc)
        our_tokens = _tokens_to_ours(total_in, total_out)

        name = data["name"]
        clean_name = re.sub(r'^я\s+', '', name.lower().strip())
        slug = _slugify(clean_name.split(',')[0].strip())
        if db.get_site_by_slug(slug):
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"

        generated_html = _inject_analytics(gen["html"], slug)
        (GENERATED_DIR / f"{slug}.html").write_text(generated_html, encoding="utf-8")
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

        services.VersionService.create_snapshot(site["id"], generated_html, data, "site_generate_included")
        db.deduct_tokens(
            user_id=user["id"],
            amount=0,
            reason=f"site_generate_included:{slug}",
            site_id=site["id"],
            claude_in=total_in,
            claude_out=total_out,
            cache_read=total_cr,
            cost_usd=cost,
        )
        db.complete_onboarding_session(session["id"], user["id"], site["id"])
        db.create_notification(
            user["id"],
            "site_created",
            "Бизнес-страница готова",
            f"Страница «{site['title']}» создана и доступна для правок.",
            site["id"],
        )
        updated_user = db.get_user_by_id(user["id"]) or user
        _save_cost({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "client": name, "slug": slug, "user_id": user["id"],
            "style": data["ref_url"] or vibe or "ai-chat",
            "chat_in": acc_chat_in, "chat_out": acc_chat_out,
            "gen_in": gen_in, "gen_out": gen_out,
            "cache_read_tokens": total_cr, "cache_create_tokens": gen_cc,
            "cost_usd": round(cost, 6), "our_tokens_spent": our_tokens,
            "included_in_site_purchase": True,
            "model": ALEM_MODEL,
        })
        return {
            "ok": True,
            "site": site,
            "site_url": f"/site/{slug}",
            "workspace_url": f"/dashboard/sites/{site['id']}",
            "cost_usd": round(cost, 6),
            "tokens_spent": our_tokens,
            "tokens_left": _dev_credits(updated_user),
            "dev_credits_left": _dev_credits(updated_user),
        }
    except Exception as exc:
        logger.exception("Onboarding generation failed")
        db.fail_onboarding_session(session["id"], user["id"], "Не удалось создать сайт")
        return {"ok": False, "status_code": 500, "error": f"Не удалось создать сайт: {exc}"}


@app.post("/chat")
async def chat(request: Request):
    # chat
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")

    body = await _json_body(request)
    message = str(body.get("message") or "").strip()
    raw_session_id = body.get("session_id")
    with open("/tmp/chat_debug.log", "a") as f:
        f.write(f"RECEIVED HISTORY LENGTH: {len(body.get('history', []))}\n")
        f.write(f"RECEIVED MESSAGE: {message}\n")
    session_id = int(raw_session_id) if str(raw_session_id or "").isdigit() else None
    if len(message) > 4000:
        return _api_error("Invalid draft payload", 400, "invalid_payload")
    history = services.OnboardingService._safe_history(body.get("history"))
    photo_urls = services.OnboardingService._safe_photo_urls(body.get("photo_urls"))
    try:
        acc_chat_in = max(0, int(body.get("chat_in") or 0))
        acc_chat_out = max(0, int(body.get("chat_out") or 0))
        acc_chat_cr = max(0, int(body.get("chat_cr") or 0))
    except (TypeError, ValueError):
        return _api_error("Invalid draft payload", 400, "invalid_payload")

    if not message:
        return _api_error("Empty message", 400, "empty_message")

    try:
        session = db.upsert_onboarding_session(user["id"], session_id)
        session_id = session["id"]
    except db.DraftLimitError:
        return _api_error("Draft limit reached", 409, "draft_limit_reached")
    except db.DraftConflictError:
        return _api_error("Draft not found", 404, "draft_not_found")

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
        session = db.upsert_onboarding_session(
            user["id"],
            session_id,
            status="draft",
            history=history,
            collected=collected,
            photo_urls=photo_urls,
            chat_in=acc_chat_in,
            chat_out=acc_chat_out,
            chat_cr=acc_chat_cr,
        )
        presented = services.OnboardingService.present(session)
        with open("/tmp/chat_debug.log", "a") as f:
            f.write(f"SENDING HISTORY LENGTH: {len(history)}\n")
        return JSONResponse({
            "message":  reply,
            "history":  history,
            "done":     False,
            "session_id": session["id"],
            "collected": collected,
            "summary": presented["summary"],
            "progress": presented["progress"],
            "chat_in":  acc_chat_in,
            "chat_out": acc_chat_out,
            "chat_cr":  acc_chat_cr,
        })

    session = db.upsert_onboarding_session(
        user["id"],
        session_id,
        status="ready",
        history=history,
        collected=collected,
        photo_urls=photo_urls,
        chat_in=acc_chat_in,
        chat_out=acc_chat_out,
        chat_cr=acc_chat_cr,
    )
    presented = services.OnboardingService.present(session)
    return JSONResponse({
        "message":      reply,
        "history":      history,
        "done":         False,
        "ready":        True,
        "confirm_required": True,
        "session_id":   session["id"],
        "collected":    collected,
        "summary":      presented["summary"],
        "progress":     presented["progress"],
        "chat_in":      acc_chat_in,
        "chat_out":     acc_chat_out,
        "chat_cr":      acc_chat_cr,
    })


@app.get("/api/onboarding/session")
async def api_onboarding_session(request: Request):
    # api onboarding session
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")
    try:
        return JSONResponse({"ok": True, **services.OnboardingService.current(user["id"])})
    except db.DraftLimitError:
        return _api_error("Draft limit reached", 409, "draft_limit_reached")


@app.post("/api/onboarding/session")
async def api_onboarding_autosave(request: Request):
    # api onboarding autosave
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")
    payload = await _json_body(request)
    with open("/tmp/chat_debug.log", "a") as f:
        f.write(f"AUTOSAVE RECEIVED HISTORY LENGTH: {len(payload.get('history', []))}\n")
        f.write(f"AUTOSAVE PAYLOAD: {payload}\n")
    try:
        return JSONResponse({"ok": True, **services.OnboardingService.autosave(user["id"], payload)})
    except db.DraftLimitError:
        return _api_error("Draft limit reached", 409, "draft_limit_reached")
    except db.DraftConflictError:
        return _api_error("Draft not found", 404, "draft_not_found")


@app.post("/api/onboarding/reset")
async def api_onboarding_reset(request: Request):
    # api onboarding reset
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")
    try:
        return JSONResponse({"ok": True, **services.OnboardingService.reset(user["id"])})
    except db.DraftLimitError:
        return _api_error("Draft limit reached", 409, "draft_limit_reached")


@app.delete("/api/onboarding/session/{session_id}")
async def api_onboarding_delete(session_id: int, request: Request):
    # api onboarding delete
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")
    result = services.OnboardingService.delete(user["id"], session_id)
    if not result.get("deleted"):
        return _api_error("Draft not found", 404, "draft_not_found")
    return JSONResponse({"ok": True, **result})


@app.patch("/api/onboarding/session/{session_id}/title")
async def api_onboarding_rename(session_id: int, request: Request):
    # api onboarding rename
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")
    payload = await _json_body(request)
    try:
        return JSONResponse({"ok": True, **services.OnboardingService.rename(user["id"], session_id, payload.get("title"))})
    except DraftValidationError:
        return _api_error("Invalid draft name", 400, "invalid_draft_name")
    except db.DraftConflictError:
        return _api_error("Draft not found", 404, "draft_not_found")


@app.patch("/api/onboarding/sessions/order")
async def api_onboarding_reorder(request: Request):
    # api onboarding reorder
    user = _require_auth(request)
    if not user:
        return _api_error("Authentication required", 401, "auth_required")
    payload = await _json_body(request)
    session_ids = payload.get("session_ids") if isinstance(payload.get("session_ids"), list) else []
    return JSONResponse({"ok": True, **services.OnboardingService.reorder(user["id"], session_ids)})


def _background_generate_task(user_id: int, session: dict):
    try:
        user = db.get_user_by_id(user_id)
        if not user:
            return
        # `_generate_site_from_session` handles marking the session completed/failed internally.
        # But we must ensure it doesn't crash silently.
        result = _generate_site_from_session(user, session)
        if not result.get("ok"):
            # It already logged error in _generate_site_from_session
            pass
    except Exception as e:
        logger.exception("Background onboarding generation task failed")
        db.fail_onboarding_session(session["id"], user_id, f"Внутренняя ошибка: {e}")

@app.post("/api/onboarding/generate")
async def api_onboarding_generate(request: Request, background_tasks: BackgroundTasks):
    # api onboarding generate
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    body = await request.json()
    session_id = int(body.get("session_id") or 0)
    session = db.get_onboarding_session(session_id, user["id"])
    if not session:
        return JSONResponse({"error": "Черновик не найден"}, status_code=404)
    if session.get("status") not in {"draft", "ready", "failed", "completed", "generating"}:
        return JSONResponse({"error": "Сначала завершите ответы и подтвердите запуск."}, status_code=400)
    
    if session.get("status") == "generating":
        return JSONResponse({"ok": True, "status": "generating"})
        
    # Mark as generating and launch background task
    with db.get_conn() as c:
        c.execute("UPDATE onboarding_sessions SET status='generating' WHERE id=? AND user_id=?", (session_id, user["id"]))
    background_tasks.add_task(_background_generate_task, user["id"], session)
    return JSONResponse({"ok": True, "status": "generating"})

@app.get("/api/onboarding/status")
async def api_onboarding_status(request: Request, session_id: int):
    # api onboarding status
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    session = db.get_onboarding_session(session_id, user["id"])
    if not session:
        return JSONResponse({"error": "Черновик не найден"}, status_code=404)
    
    status = session.get("status")
    res = {"ok": True, "status": status}
    
    if status == "completed" and session.get("generated_site_id"):
        site = db.get_site_by_id(session["generated_site_id"])
        if site and site["user_id"] == user["id"]:
            res["workspace_url"] = f"/dashboard/sites/{site['id']}"
    elif status == "failed":
        res["error"] = session.get("error") or "Не удалось создать сайт"
        
    return JSONResponse(res)


# ── Site edit ─────────────────────────────────────────────────────────────────

@app.post("/site/{slug}/edit")
async def site_edit(slug: str, request: Request):
    # handle ai site edit and generation flow
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    site = services.SupportService.refresh_site(site["id"]) or site
    if not services.is_support_operational(site.get("support_status")):
        return JSONResponse({"error": "Поддержка сайта не активна. Оплатите поддержку, чтобы редактировать сайт."}, status_code=402)

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

    ai_message = message
    if new_photo_urls:
        ai_message += f"\n[Системное уведомление: клиент прикрепил {len(new_photo_urls)} фото к сообщению]"
    
    # We still want to save the original message in history without the system tag,
    # but for the AI prompt we'll pass the ai_message
    ai_history = edit_history + [{"role": "user", "content": ai_message}]
    edit_history = edit_history + [{"role": "user", "content": message}]

    result       = _ai_edit_chat(ai_history, site_context)
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
    if _dev_credits(user) < 1:
        return JSONResponse({"error": "Недостаточно кредитов разработки"}, status_code=402)

    business_check = services.PromotionService.validate_business_change(site, edit_summary)
    if not business_check.get("ok"):
        return JSONResponse({"error": business_check["message"]}, status_code=400)

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

    fresh_user = db.get_user_by_id(user["id"]) or user
    if _dev_credits(fresh_user) < our_tokens:
        return JSONResponse({"error": "Недостаточно кредитов разработки"}, status_code=402)

    if prev_html:
        services.VersionService.create_snapshot(site["id"], prev_html, site.get("data") or {}, "before_site_edit")
    deducted = db.deduct_tokens(
        user_id=user["id"], amount=our_tokens,
        reason=f"site_edit:{slug}",
        site_id=site["id"],
        claude_in=gen_in, claude_out=gen_out,
        cache_read=gen_cr, cost_usd=cost,
    )
    if not deducted:
        return JSONResponse({"error": "Баланс изменился. Пополните кредиты разработки и попробуйте ещё раз."}, status_code=409)

    updated_html = _inject_analytics(gen["html"], slug)
    (GENERATED_DIR / f"{slug}.html").write_text(updated_html, encoding="utf-8")

    data_to_save = {**data, "chat_history": combined_history}
    db.update_site_data(site["id"], data_to_save)
    db.update_site_html(site["id"], str(GENERATED_DIR / f"{slug}.html"), our_tokens)
    services.VersionService.create_snapshot(site["id"], updated_html, data_to_save, "site_edit")
    services.CampaignService.site_changed(site["id"], "site_edit")
    updated_user = db.get_user_by_id(user["id"]) or user

    return JSONResponse({
        "done":         True,
        "ok":           True,
        "message":      reply,
        "site_url":     f"/site/{slug}",
        "workspace_url": f"/dashboard/sites/{site['id']}",
        "edit_history": edit_history,
        "tokens_spent": our_tokens,
        "tokens_left":  _dev_credits(updated_user),
        "dev_credits_left": _dev_credits(updated_user),
    })


# ── Payment routes ────────────────────────────────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    # profile page
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    log = db.get_dev_credit_log(user["id"])
    promo_log = db.get_promo_credit_log(user["id"])
    sites = db.get_user_sites(user["id"])
    csrf_token = auth_services.CsrfService.generate()
    response = templates.TemplateResponse(request, "profile.html", {
        "user": user,
        "log": log,
        "promo_log": promo_log,
        "sites_count": len(sites),
        "verification_notice": _verification_notice(request, user),
        "csrf_token": csrf_token,
    })
    _set_auth_csrf_cookie(response, request, csrf_token)
    return response


@app.post("/profile/update")
async def profile_update(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    avatar_url: str = Form(""),
    csrf_token: str = Form(""),
):
    # profile update
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    try:
        _verify_auth_csrf(request, csrf_token)
        safe_name = auth_services.validate_name(name)
    except auth_services.AuthError:
        return RedirectResponse("/profile?email_error=verification_failed", status_code=302)
    db.update_user_name(user["id"], safe_name)

    if avatar_url:
        db.update_user_avatar(user["id"], avatar_url)

@app.post("/profile/update-password")
async def profile_update_password(
    request: Request,
    password: str = Form(...),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    # profile update password
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    try:
        _verify_auth_csrf(request, csrf_token)
        auth_services.validate_password(password, confirm_password, email=user.get("email"), name=user.get("name"))
    except auth_services.AuthError as e:
        return RedirectResponse(f"/profile?password_error={e.code}", status_code=302)

    db.update_user_password(user["id"], password)
    return RedirectResponse("/profile?password_success=updated", status_code=302)


    try:
        new_email = auth_services.validate_email(email) if (email or "").strip() else ""
    except auth_services.AuthError:
        return RedirectResponse("/profile?email_error=invalid_email", status_code=302)
    current_email = auth_services.normalize_email(user.get("email"))
    if new_email and new_email != current_email:
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
    # site delete
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


def _owned_site(slug: str, user: dict) -> dict | None:
    # owned site
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return None
    return site


@app.post("/api/billing/promo-credits/purchase")
async def api_purchase_promo_credits(request: Request):
    # api purchase promo credits
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    body = await request.json()
    try:
        credits = int(body.get("credits") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_amount", "message": "Введите количество кредитов."}, status_code=400)
    if credits < PROMO_MIN_PURCHASE:
        return JSONResponse({
            "ok": False,
            "error": "min_amount",
            "message": f"Минимальное пополнение - {PROMO_MIN_PURCHASE} кредитов продвижения.",
        }, status_code=400)

    phone_clean = re.sub(r"[^\d]", "", body.get("phone") or user.get("phone") or "")
    if len(phone_clean) < 10:
        return JSONResponse({"ok": False, "error": "phone_required", "message": "Введите номер телефона Kaspi."}, status_code=400)

    amount = credits * PROMO_CREDIT_TENGE
    order_id = _payment_order_id()
    try:
        data = _kaspi_invoice(
            phone_clean,
            order_id,
            f"lendings.kz {credits} кредитов продвижения",
            amount=amount,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Ошибка платежного шлюза: {exc}"}, status_code=502)
    if not data.get("id"):
        return JSONResponse({"ok": False, "error": "Kaspi не принял платёж", "detail": data}, status_code=400)
    db.create_payment(
        user_id=user["id"],
        order_id=order_id,
        invoice_id=str(data["id"]),
        amount=amount,
        tokens=0,
        status="pending",
        payment_kind="promo_credits",
        dev_credits=0,
        promo_credits=credits,
    )
    return JSONResponse({
        "ok": True,
        "invoice_id": data["id"],
        "order_id": order_id,
        "credits": credits,
        "amount": amount,
        "message": f"Запрос отправлен на номер +{phone_clean}. Откройте Kaspi и подтвердите оплату.",
    })


@app.get("/api/billing/credit-logs")
async def api_credit_logs(request: Request):
    # api credit logs
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    return JSONResponse(services.CreditsService.logs(user["id"]))


@app.get("/api/sites/{slug}/support")
async def api_support_status(slug: str, request: Request):
    # api support status
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    site = services.SupportService.refresh_site(site["id"]) or site
    return JSONResponse({
        "ok": True,
        "status": site.get("support_status"),
        "support_paid_until": site.get("support_paid_until"),
        "invoice": services.SupportService.get_open_invoice(site["id"]),
    })


@app.post("/api/sites/{slug}/analytics/events")
async def api_site_analytics_event(slug: str, request: Request):
    # public endpoint used by generated sites
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = db.get_site_by_slug(slug)
    if not site:
        return JSONResponse({"ok": False}, status_code=404)
    site = services.SupportService.refresh_site(site["id"]) or site
    try:
        body = await request.json()
    except Exception:
        body = {}
    services.AnalyticsService.record_event(
        site["id"],
        body.get("event_type") or "cta_click",
        body.get("payload") or {},
    )
    return JSONResponse({"ok": True})


@app.post("/api/sites/{slug}/support/pay")
async def api_support_pay(slug: str, request: Request):
    # api support pay
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    site = services.SupportService.refresh_site(site["id"]) or site
    invoice = services.SupportService.get_open_invoice(site["id"])
    if not invoice:
        return JSONResponse({"ok": False, "error": "support_active", "message": "Поддержка уже активна."}, status_code=400)
    body = await request.json()
    phone_clean = re.sub(r"[^\d]", "", body.get("phone") or user.get("phone") or "")
    if len(phone_clean) < 10:
        return JSONResponse({"ok": False, "error": "phone_required", "message": "Введите номер телефона Kaspi."}, status_code=400)
    order_id = _payment_order_id()
    try:
        data = _kaspi_invoice(
            phone_clean,
            order_id,
            f"lendings.kz поддержка сайта {site['slug']}",
            amount=int(invoice["amount"]),
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Ошибка платежного шлюза: {exc}"}, status_code=502)
    if not data.get("id"):
        return JSONResponse({"ok": False, "error": "Kaspi не принял платёж", "detail": data}, status_code=400)
    db.create_payment(
        user_id=user["id"],
        order_id=order_id,
        invoice_id=str(data["id"]),
        amount=int(invoice["amount"]),
        tokens=0,
        status="pending",
        payment_kind="support_invoice",
        dev_credits=0,
        promo_credits=0,
        site_id=site["id"],
        support_invoice_id=invoice["id"],
    )
    return JSONResponse({
        "ok": True,
        "invoice_id": data["id"],
        "order_id": order_id,
        "amount": int(invoice["amount"]),
        "message": f"Запрос отправлен на номер +{phone_clean}. Откройте Kaspi и подтвердите оплату.",
    })


@app.post("/api/sites/{slug}/promotion/setup")
async def api_promotion_setup(slug: str, request: Request):
    # api promotion setup
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    result = services.PromotionService.setup(user["id"], site["id"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/sites/{slug}/promotion/forecast")
async def api_promotion_forecast(slug: str, request: Request):
    # api promotion forecast
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    body = await request.json()
    try:
        credits = int(body.get("credits") or 0)
        duration_hours = int(body.get("duration_hours") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_campaign", "message": "Введите корректный бюджет и длительность."}, status_code=400)
    result = services.CampaignService.forecast(
        user["id"],
        site["id"],
        credits,
        duration_hours,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/sites/{slug}/promotion/campaigns")
async def api_campaign_launch(slug: str, request: Request):
    # api campaign launch
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    body = await request.json()
    try:
        credits = int(body.get("credits") or 0)
        duration_hours = int(body.get("duration_hours") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_campaign", "message": "Введите корректный бюджет и длительность."}, status_code=400)
    result = services.CampaignService.launch(
        user["id"],
        site["id"],
        credits,
        duration_hours,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/sites/{slug}/promotion/campaigns")
async def api_campaign_history(slug: str, request: Request):
    # api campaign history
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    return JSONResponse({"ok": True, "campaigns": services.CampaignService.history(site["id"])})


@app.get("/api/sites/{slug}/promotion/campaigns/{campaign_id}")
async def api_campaign_status(slug: str, campaign_id: int, request: Request):
    # api campaign status
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    campaigns = services.CampaignService.history(site["id"])
    campaign = next((c for c in campaigns if int(c["id"]) == int(campaign_id)), None)
    if not campaign:
        return JSONResponse({"error": "Кампания не найдена"}, status_code=404)
    return JSONResponse({"ok": True, "campaign": campaign})
@app.post("/api/sites/{slug}/analytics/purchase")
async def api_purchase_analytics(slug: str, request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
        
    if site["data"].get("analytics_purchased"):
        return JSONResponse({"error": "Уже подключено"}, status_code=400)
        
    price = 200
    fresh_user = db.get_user_by_id(user["id"])
    if (fresh_user.get("dev_credits") or 0) < price:
        return JSONResponse({"error": "Недостаточно кредитов разработки"}, status_code=402)
        
    deducted = db.deduct_tokens(
        user_id=user["id"], amount=price,
        reason=f"analytics_purchase:{slug}",
        site_id=site["id"],
        claude_in=0, claude_out=0, cache_read=0, cost_usd=0.0
    )
    if not deducted:
        return JSONResponse({"error": "Не удалось списать кредиты"}, status_code=409)
        
    data = site["data"]
    data["analytics_purchased"] = True
    db.update_site_data(site["id"], data)
    
    return JSONResponse({"ok": True})


@app.post("/api/sites/{slug}/analytics/restore")
async def api_restore_analytics(slug: str, request: Request):
    # api restore analytics
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    result = services.AnalyticsService.restore(user["id"], site["id"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/sites/{slug}/versions")
async def api_site_versions(slug: str, request: Request):
    # api site versions
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    return JSONResponse({"ok": True, "versions": services.VersionService.list_versions(user["id"], site["id"])})


@app.post("/api/sites/{slug}/versions/{version_id}/restore")
async def api_restore_version(slug: str, version_id: int, request: Request):
    # api restore version
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    result = services.VersionService.restore(user["id"], site["id"], version_id)
    if result.get("ok"):
        restored_html = _inject_analytics(result["html"], site["slug"])
        (GENERATED_DIR / f"{site['slug']}.html").write_text(restored_html, encoding="utf-8")
        db.update_site_html(site["id"], str(GENERATED_DIR / f"{site['slug']}.html"), site.get("tokens_used") or 0)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/payment", response_class=HTMLResponse)
async def payment_page(request: Request):
    # payment page
    user = _require_auth(request)
    reason = request.query_params.get("reason", "")
    sites  = db.get_user_sites(user["id"]) if user else []
    slot_pkg    = next(p for p in PAYMENT_PACKAGES if p["type"] == "slot")
    credit_pkgs = [p for p in PAYMENT_PACKAGES if p["type"] == "credits"]
    return templates.TemplateResponse(request, "payment.html", {
        "user":        user,
        "reason":      reason,
        "sites_count": len(sites),
        "slot_pkg":    slot_pkg,
        "credit_pkgs": credit_pkgs,
        "promo_min_purchase": PROMO_MIN_PURCHASE,
        "promo_credit_tenge": PROMO_CREDIT_TENGE,
        "verification_notice": _verification_notice(request, user) if user else {},
    })


@app.post("/payment/create")
async def payment_create(request: Request):
    # payment create
    user = _require_auth(request)
    body = await request.json()
    catalog_item_id = body.get("catalog_item_id")
    phone = body.get("phone", "").strip()
    promo_code = body.get("promo_code", "").strip()

    pkg = next((p for p in PAYMENT_PACKAGES if p["catalog_item_id"] == catalog_item_id), None)
    if not pkg:
        return JSONResponse({"error": "Неверный пакет"}, status_code=400)

    is_promo_free = (promo_code.lower() == "test67")

    phone_clean = re.sub(r"[^\d]", "", phone)
    if not is_promo_free and len(phone_clean) < 10:
        return JSONResponse({"error": "Введите номер телефона Kaspi"}, status_code=400)

    # Guest auto-login/auto-create
    is_new_session = False
    if not user:
        if len(phone_clean) < 10:
            return JSONResponse({"error": "Укажите телефон для привязки аккаунта"}, status_code=400)
        
        user = db.get_user_by_phone(phone_clean)
        if not user:
            user = db.create_user(phone=phone_clean, password="")
            if not user:
                return JSONResponse({"error": "Ошибка при создании аккаунта"}, status_code=500)
        is_new_session = True

    order_id = _payment_order_id()

    if is_promo_free:
        data = {"id": "promo_" + order_id}
    else:
        try:
            data = _kaspi_invoice(
                phone_clean,
                order_id,
                f"lendings.kz {pkg['label']}",
                catalog_item_id=catalog_item_id,
            )
        except Exception as e:
            return JSONResponse({"error": f"Ошибка платежного шлюза: {e}"}, status_code=502)

        if not data.get("id"):
            return JSONResponse({"error": "Kaspi не принял платёж", "detail": data}, status_code=400)

    db.create_payment(
        user_id=user["id"],
        order_id=order_id,
        invoice_id=str(data["id"]),
        amount=0 if is_promo_free else pkg["price"],
        tokens=pkg["tokens"],
        status="pending",
        catalog_item_id=catalog_item_id,
        payment_kind="site_slot" if pkg["type"] == "slot" else "dev_credits",
        dev_credits=pkg["tokens"],
        promo_credits=0,
    )

    if is_promo_free or user["id"] == 7:
        payment = db.get_payment_by_order(order_id)
        if payment:
            db.complete_payment(payment["id"])
            kind = payment.get("payment_kind") or "legacy"
            if kind == "site_slot":
                db.add_site_slot(payment["user_id"], int(payment.get("dev_credits") or payment["tokens"] or 0), f"slot_purchase:{order_id}")
            elif kind == "dev_credits":
                db.add_tokens(payment["user_id"], int(payment.get("dev_credits") or payment["tokens"] or 0), f"credits_purchase:{order_id}")

    return JSONResponse({
        "ok": True,
        "invoice_id": data["id"],
        "order_id": order_id,
        "message": "Промокод применён! Завершаем..." if is_promo_free else f"Запрос отправлен на номер +{phone_clean}. Откройте Kaspi и подтвердите оплату.",
    })


@app.get("/payment/status/{order_id}")
async def payment_status(order_id: str, request: Request):
    # payment status
    order_id = re.sub(r"[^A-Za-z0-9]", "", order_id)
    payment = db.get_payment_by_order(order_id)
    if not payment:
        return JSONResponse({"error": "Не найдено"}, status_code=404)

    user = _require_auth(request)
    if user and payment["user_id"] != user["id"]:
        return JSONResponse({"error": "Не найдено"}, status_code=404)

    response = JSONResponse({
        "status": payment["status"],
        "tokens": payment["tokens"],
        "dev_credits": payment.get("dev_credits") or payment["tokens"],
        "promo_credits": payment.get("promo_credits") or 0,
        "payment_kind": payment.get("payment_kind") or "legacy",
    })

    if payment["status"] == "paid" and not user:
        sid = auth_services.SessionService.create(payment["user_id"])
        _set_session_cookie(response, request, sid)

    return response


@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    # payment webhook
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
        kind = payment.get("payment_kind") or "legacy"
        if kind == "site_slot":
            db.add_site_slot(payment["user_id"], int(payment.get("dev_credits") or payment["tokens"] or 0), f"slot_purchase:{order_id}")
        elif kind == "dev_credits":
            db.add_tokens(payment["user_id"], int(payment.get("dev_credits") or payment["tokens"] or 0), f"credits_purchase:{order_id}")
        elif kind == "promo_credits":
            services.CreditsService.apply_promo_payment(payment)
        elif kind == "support_invoice":
            services.SupportService.mark_invoice_paid(payment)
        else:
            pkg = next((p for p in PAYMENT_PACKAGES if p["catalog_item_id"] == payment.get("catalog_item_id")), None)
            if pkg and pkg.get("type") == "slot":
                db.add_site_slot(payment["user_id"], payment["tokens"], f"slot_purchase:{order_id}")
            else:
                db.add_tokens(payment["user_id"], payment["tokens"], f"credits_purchase:{order_id}")
    elif ev_type in ("payment.failed", "payment.expired"):
        db.fail_payment(payment["id"], ev_type)

    return JSONResponse({"ok": True})
