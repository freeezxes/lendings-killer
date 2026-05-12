from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
import os, re, uuid, json, time, base64, hashlib, hmac
from pathlib import Path
import anthropic
import httpx
import db

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

# ── Kaspi Pay via kaspi-pos on astana-gb server ───────────────────────────────
KASPI_POS_URL    = "http://92.38.49.113:4001"
KASPI_API_KEY    = "lendings-kaspi-key"
KASPI_WH_SECRET  = "b8daafada57acef22720443606cacb441bc4bd0228b6374f627a8b75d474edf0"

# catalog item ids → token amounts
PAYMENT_PACKAGES = [
    {"catalog_item_id": "17785735222608682", "tokens": 100,  "price": 990,  "label": "100 токенов — 990 ₸"},
    {"catalog_item_id": "17785735222608784", "tokens": 300,  "price": 2490, "label": "300 токенов — 2 490 ₸"},
    {"catalog_item_id": "17785735222608267", "tokens": 50,   "price": 1990, "label": "Старт (1 сайт) — 1 990 ₸"},
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


def _ai_edit_chat(history: list) -> dict:
    """Single turn of edit dialogue — clarifies request before generating."""
    resp = ai_client.messages.create(
        model=BEDROCK_MODEL,
        max_tokens=256,
        system=[{"type": "text", "text": EDIT_CHAT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=history,
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"reply": raw, "ready": False, "edit_summary": None}


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

EDIT_CHAT_SYSTEM = """Ты — помощник по редактированию сайта-визитки. Клиент хочет что-то изменить на своём сайте.

Твоя задача — понять запрос и при необходимости уточнить детали перед тем как отдать его в работу.

Правила:
- Если запрос ЧЁТКИЙ и конкретный (поменяй цвет на синий, добавь Instagram @name, измени цену на 5000) — сразу подтверди и ставь ready:true
- Если запрос РАЗМЫТЫЙ (сделай красивее, улучши, переделай) — задай 1 уточняющий вопрос: что именно? как должно выглядеть?
- Если несколько изменений — подтверди каждое коротко и ставь ready:true
- Не задавай больше 1 вопроса за раз
- Пиши коротко, на «ты», без лишних слов

Примеры:
- «поменяй адрес на Алматы» → ready:true, «Понял, меняю адрес на Алматы ✓»
- «сделай более официально» → ready:false, «Что именно сделать официальнее — шрифт, цвета, тексты?»
- «добавь скидку 20% на маникюр» → ready:true, «Добавляю скидку 20% на маникюр ✓»
- «переделай сайт» → ready:false, «В каком направлении переделать? Например: другие цвета, другой стиль, новые секции?»

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
    if not user:
        return RedirectResponse("/auth", status_code=302)
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
    return templates.TemplateResponse(request, "auth.html")


@app.post("/auth/register")
async def auth_register(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
):
    # Normalise phone: keep digits only, strip leading +
    phone = re.sub(r'[^\d]', '', phone)
    if not phone:
        return templates.TemplateResponse(request, "auth.html",
                                          {"error": "Неверный номер телефона"}, status_code=400)

    user = db.create_user(phone, password, name.strip())
    if user is None:
        return templates.TemplateResponse(request, "auth.html",
                                          {"error": "Этот номер уже зарегистрирован"}, status_code=400)

    sid = db.create_session(user["id"])
    response = RedirectResponse("/create", status_code=302)
    response.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=365 * 24 * 3600)
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
                                          {"error": "Неверный номер или пароль"}, status_code=401)

    sid = db.create_session(user["id"])
    response = RedirectResponse("/create", status_code=302)
    response.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=365 * 24 * 3600)
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    sid = request.cookies.get("sid")
    if sid:
        db.delete_session(sid)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("sid")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = _require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    sites = db.get_user_sites(user["id"])
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "sites": sites})


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
    edit_history   = body.get("edit_history", [])   # separate edit dialogue history
    client_history = body.get("history", [])        # original creation history

    if not message:
        return JSONResponse({"error": "Пустой запрос"}, status_code=400)

    # Build edit dialogue history
    edit_history = edit_history + [{"role": "user", "content": message}]

    # Ask edit-chat AI: clarify or approve?
    result      = _ai_edit_chat(edit_history)
    reply       = result.get("reply", "Понял!")
    ready       = result.get("ready", False)
    edit_summary = result.get("edit_summary") or message

    edit_history = edit_history + [{"role": "assistant", "content": reply}]

    # Not ready yet — ask clarifying question
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

    stored_history  = data.get("chat_history", [])
    combined_history = client_history if client_history else stored_history

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
    })


@app.post("/profile/update")
async def profile_update(request: Request, name: str = Form(...)):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    db.update_user_name(user["id"], name)
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
    return templates.TemplateResponse(request, "payment.html", {
        "user": user,
        "packages": PAYMENT_PACKAGES,
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
        db.add_tokens(payment["user_id"], payment["tokens"], f"kaspi_payment:{order_id}")
    elif ev_type in ("payment.failed", "payment.expired"):
        db.fail_payment(payment["id"], ev_type)

    return JSONResponse({"ok": True})
