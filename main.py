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
ALEM_MODEL = settings.alem_model

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

OCR_API_KEY = settings.ocr_api_key

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
    # fetch reference site styles via Playwright script
    import subprocess
    import json
    
    if not url.startswith("http"):
        url = "https://" + url

    try:
        result = subprocess.run(
            ["venv_playwright/bin/python", "scripts/playwright_scraper.py", url],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.error(f"Playwright scraper error: {result.stderr}")
            return ""
            
        tokens = json.loads(result.stdout)
        if "error" in tokens:
            logger.error(f"Playwright scraper JSON error: {tokens['error']}")
            return ""
    except Exception as e:
        logger.error(f"Failed to run playwright scraper: {e}")
        return ""

    lines = [f"=== ДИЗАЙН-БРИФ: {url} ===\n"]

    if tokens.get("google_fonts_urls"):
        lines.append("ПОДКЛЮЧИ ЭТИ ШРИФТЫ (вставь в <head>):")
        for u in tokens["google_fonts_urls"]:
            lines.append(f'  <link href="{u}" rel="stylesheet">')

    if tokens.get("fonts"):
        lines.append(f"\nШРИФТЫ: {', '.join(tokens['fonts'])}")

    if tokens.get("colors"):
        lines.append(f"\nТЕКСТОВЫЕ ЦВЕТА САЙТА: {', '.join(tokens['colors'])}")

    if tokens.get("backgrounds"):
        lines.append(f"\nФОНЫ САЙТА (Используй их для секций/карточек): {'; '.join(tokens['backgrounds'])}")

    if tokens.get("border_radius"):
        lines.append(f"\nСКРУГЛЕНИЯ (border-radius): {', '.join(tokens['border_radius'])}")

    if tokens.get("shadows"):
        lines.append(f"\nТЕНИ (box-shadow): {'; '.join(tokens['shadows'])}")

    lines.append("\nВАЖНО: Создай свои CSS-переменные (--primary, --bg, --text) на основе этих реальных вычисленных цветов, чтобы сайт выглядел точно так же.")

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
        user_content = f"""Вот ТЕКУЩИЙ HTML сайта клиента:

=== ТЕКУЩИЙ HTML ===
{prev_html_full}

=== ЗАПРОС КЛИЕНТА ===
«{edit_request}»

Твоя задача — вернуть СТРОГО валидный JSON-массив объектов замен, чтобы выполнить точечное редактирование HTML кода.
Формат:
[
  {{
    "find": "точный кусок старого кода (желательно несколько строк для уникальности)",
    "replace": "новый код, на который нужно заменить"
  }}
]
Верни ТОЛЬКО JSON, больше ничего. Никакого markdown. Не переписывай весь HTML, верни только нужные блоки замен. Убедись, что строка в 'find' 100% совпадает с куском из оригинального HTML, включая отступы и пробелы!"""

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
    content  = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

    if edit_request and prev_html_full:
        import json
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        content = content.replace("```", "").strip()
        
        try:
            patches = json.loads(content)
            html = prev_html_full
            for patch in patches:
                find_str = patch.get("find", "")
                replace_str = patch.get("replace", "")
                if find_str and find_str in html:
                    html = html.replace(find_str, replace_str)
            if html == prev_html_full:
                logger.warning("AI edit JSON patch returned matches that were not found in original HTML")
        except json.JSONDecodeError:
            logger.exception("AI edit JSON patch failed to decode")
            html = prev_html_full
    else:
        html = content
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

async def _agent_generate(data: dict, slug: str) -> dict:
    import openhands_client
    
    ref_url = data.get("ref_url", "").strip()
    vibe    = data.get("vibe", "").strip()
    extra   = data.get("extra", "").strip()
    edit_request = data.get("edit_request", "").strip()

    prompt_lines = []
    
    if edit_request:
        prompt_lines.append(f"ЗАПРОС НА ПРАВКУ СУЩЕСТВУЮЩЕГО КОДА:\n{edit_request}")
        prompt_lines.append("Внимательно изучи существующий код в директории src/, исправь ошибки или добавь запрошенный функционал.")
    else:
        prompt_lines.append("ЗАДАЧА: Создать новый красивый лендинг.")
        prompt_lines.append(f"Данные бизнеса:\n- Имя: {data.get('name')}\n- Услуги: {data.get('services')}\n- Город: {data.get('city')}")
        
        if ref_url:
            from main import _fetch_url
            brief = _fetch_url(ref_url)
            if brief:
                prompt_lines.append(f"Дизайн-бриф ({ref_url}):\n{brief}")
                
        if vibe:
            prompt_lines.append(f"Пожелания по стилю: {vibe}")
            
        if extra:
            prompt_lines.append(f"Дополнительно: {extra}")
            
        prompt_lines.append(f"ТРЕБОВАНИЯ:\n1. ТЫ ДОЛЖЕН СДЕЛАТЬ ВСЁ БЫСТРО! Сохрани весь код СТРОГО в файл по пути `/workspace/{slug}/index.html`.\n2. ВАЖНО: Никакого React, Vite, Node.js или npm install! Используй чистый HTML5 и Tailwind CSS через CDN (`<script src=\"https://cdn.tailwindcss.com\"></script>`).\n3. ДИЗАЙН: Напиши красивый, современный UI (используй Phosphor Icons через CDN). ВАЖНО: Приложение должно занимать ВСЮ ширину экрана (100vw). Никаких черных или пустых полос по бокам! Делай премиальный вид: карточки с тенями (shadow-lg), красивые скругления (rounded-2xl), современные градиенты, отступы (padding).\n4. Как только сохранишь код в `/workspace/{slug}/index.html`, СРАЗУ ЖЕ завершай работу.")

    prompt = "\n\n".join(prompt_lines)
    
    # Ensure the directory exists so OpenHands can write to it
    (GENERATED_DIR / slug).mkdir(exist_ok=True)
    
    success = await openhands_client.run_openhands_task(slug, prompt)
    
    # Generate dummy HTML to satisfy legacy db/routing until we refactor routing fully
    dummy_html = f"<!DOCTYPE html><html><head><meta http-equiv='refresh' content='0; url=/static/sites/{slug}/index.html'></head><body>Loading App...</body></html>"
    
    return {
        "html": dummy_html if success else "<html><body>Error generating site</body></html>",
        "input_tokens": 10000, # Stub for billing
        "output_tokens": 2000, # Stub for billing
        "cache_read_tokens": 0,
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














