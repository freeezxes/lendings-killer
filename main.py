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

ADMIN_PHONE = "77777777777"

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

    user_content = f"""Данные клиента:
- Имя/профессия: {data.get('name', '')}
- Услуги и цены: {data.get('services', '')}
- Город и контакт: {data.get('city', '')}
{photos_block}

=== СТИЛЬ И ДИЗАЙН ===
{style_block}

Сгенерируй полный HTML сайт-визитку для этого клиента."""

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
    """1K claude tokens = 1 our token."""
    return max(1, round((inp + out) / 1000))


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

ONBOARDING_STEPS = [
    {"id": "name",     "question": "Как тебя зовут и чем занимаешься? Например: «Я Айгуль, мастер маникюра»"},
    {"id": "services", "question": "Какие услуги ты оказываешь? Перечисли с ценами как удобно"},
    {"id": "city",     "question": "В каком городе работаешь и как к тебе записаться? (WhatsApp, Telegram или телефон)"},
    {"id": "photos",   "question": "Добавь фотки своих работ — они появятся на сайте! Можно загрузить несколько. Или напиши «пропустить»"},
    {"id": "vibe",     "question": "Как должен чувствовать себя посетитель на твоём сайте?\nНапример: уютно и тепло / дорого и стильно / свежо и современно / строго и профессионально\n\nМожешь описать своими словами — или скинуть ссылку на сайт чей дизайн тебе нравится"},
    {"id": "extra",    "question": "Хочешь что-то добавить? Любимые цвета, пожелания по стилю, чего точно не хочешь — или просто напиши «всё ок»"},
]


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
    return templates.TemplateResponse(request, "index.html", {"user": user})


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

@app.post("/chat")
async def chat(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    body   = await request.json()
    step   = body.get("step", 0)
    answer = body.get("answer", "")
    data   = body.get("data", {})

    if step < len(ONBOARDING_STEPS):
        data[ONBOARDING_STEPS[step]["id"]] = answer

    next_step = step + 1

    if next_step < len(ONBOARDING_STEPS):
        return JSONResponse({
            "message": ONBOARDING_STEPS[next_step]["question"],
            "step":    next_step,
            "data":    data,
            "done":    False,
        })

    # Final step — generate site
    vibe  = data.get("vibe", "").strip()
    extra = data.get("extra", "").strip()

    ref_url = ""
    for field in [vibe, extra]:
        if _is_url(field):
            ref_url = field
            break
    data["ref_url"] = ref_url

    # Check token balance before generating
    if user["tokens"] < 1:
        return JSONResponse({"error": "Недостаточно токенов для генерации сайта"}, status_code=402)

    result = _ai_generate(data)

    inp = result["input_tokens"]
    out = result["output_tokens"]
    cr  = result["cache_read_tokens"]
    cc  = result["cache_create_tokens"]
    cost = _calc_cost(inp, out, cr, cc)

    # Token accounting: 1K claude tokens = 1 our token
    our_tokens = _tokens_to_ours(inp, out)

    # Build slug from client name
    name = data.get("name", "site")
    clean_name = re.sub(r'^я\s+', '', name.lower().strip())
    slug = _slugify(clean_name.split(',')[0].strip())

    # Ensure slug is unique for this user by appending short uid if needed
    existing = db.get_site_by_slug(slug)
    if existing and existing.get("user_id") != user["id"]:
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"

    html_path = str(GENERATED_DIR / f"{slug}.html")
    (GENERATED_DIR / f"{slug}.html").write_text(result["html"], encoding="utf-8")

    # Persist to DB
    site = db.create_site(
        user_id=user["id"],
        slug=slug,
        title=name,
        data=data,
        html_path=html_path,
        tokens_used=our_tokens,
    )

    # Deduct tokens
    db.deduct_tokens(
        user_id=user["id"],
        amount=our_tokens,
        reason=f"site_generate:{slug}",
        site_id=site["id"] if site else None,
        claude_in=inp,
        claude_out=out,
        cache_read=cr,
        cost_usd=cost,
    )

    # Legacy cost log
    _save_cost({
        "ts":                  time.strftime("%Y-%m-%d %H:%M:%S"),
        "client":              name,
        "slug":                slug,
        "user_id":             user["id"],
        "style":               data.get("ref_url") or data.get("vibe") or "random",
        "input_tokens":        inp,
        "output_tokens":       out,
        "cache_read_tokens":   cr,
        "cache_create_tokens": cc,
        "cost_usd":            round(cost, 6),
        "our_tokens_spent":    our_tokens,
        "model":               BEDROCK_MODEL,
    })

    return JSONResponse({
        "message":         "Сайт готов!",
        "step":            next_step,
        "data":            data,
        "done":            True,
        "site_url":        f"/site/{slug}",
        "cost_usd":        round(cost, 6),
        "tokens_spent":    our_tokens,
        "tokens_left":     user["tokens"] - our_tokens,
    })


@app.get("/start")
async def start(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    return JSONResponse({
        "message": ONBOARDING_STEPS[0]["question"],
        "step": 0, "data": {}, "done": False,
    })


# ── Payment routes ────────────────────────────────────────────────────────────

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
