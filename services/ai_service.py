import re, json, httpx, uuid, base64
from core.config import settings
import logging
logger = logging.getLogger(__name__)

ALEM_API_KEY = settings.alem_api_key
ALEM_API_URL = settings.alem_api_url
ALEM_MODEL = settings.alem_model
from core.prompts import (
    PRICE_INPUT,
    PRICE_OUTPUT,
    SYSTEM_PROMPT,
    EDIT_CHAT_SYSTEM,
    CHAT_SYSTEM,
)
from pathlib import Path
GENERATED_DIR = Path('generated_sites')

async def _ask_llm(model: str, max_tokens: int, system_text: str, messages: list) -> dict:
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
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(ALEM_API_URL, headers=headers, json=payload)
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



async def _fetch_url(url: str) -> str:
    # fetch reference site styles via Playwright natively
    import json
    from playwright.async_api import async_playwright
    
    if not url.startswith("http"):
        url = "https://" + url

    tokens = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                # Wait until network is mostly idle to ensure SPA renders
                await page.goto(url, timeout=15000, wait_until="networkidle")
            except Exception:
                # If networkidle times out, just proceed with what we have
                pass

            js_script = """
            () => {
                const elements = document.querySelectorAll('*');
                const colors = new Set();
                const bgColors = new Set();
                const fonts = new Set();
                const radii = new Set();
                const shadows = new Set();

                elements.forEach(el => {
                    const style = window.getComputedStyle(el);
                    
                    if (style.color && style.color !== 'rgba(0, 0, 0, 0)') colors.add(style.color);
                    if (style.backgroundColor && style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.backgroundColor !== 'transparent') bgColors.add(style.backgroundColor);
                    if (style.fontFamily) fonts.add(style.fontFamily);
                    if (style.borderRadius && style.borderRadius !== '0px') radii.add(style.borderRadius);
                    if (style.boxShadow && style.boxShadow !== 'none') shadows.add(style.boxShadow);
                });
                
                const googleFonts = [];
                document.querySelectorAll('link[href*="fonts.googleapis.com"]').forEach(link => {
                    googleFonts.push(link.href);
                });

                return {
                    colors: Array.from(colors).slice(0, 15),
                    backgrounds: Array.from(bgColors).slice(0, 10),
                    fonts: Array.from(fonts).map(f => f.split(',')[0].replace(/['"]/g, '').trim()).slice(0, 5),
                    border_radius: Array.from(radii).slice(0, 5),
                    shadows: Array.from(shadows).slice(0, 5),
                    google_fonts_urls: googleFonts.slice(0, 3)
                };
            }
            """
            tokens = await page.evaluate(js_script)
            await browser.close()
    except Exception as e:
        logger.error(f"Playwright native error: {e}")
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



async def _ai_generate(data: dict) -> dict:
    # generate complete html site via ai
    ref_url = data.get("ref_url", "").strip()
    vibe    = data.get("vibe", "").strip()
    extra   = data.get("extra", "").strip()

    style_lines = []

    if ref_url and _is_url(ref_url):
        brief = await _fetch_url(ref_url)
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

    resp = await _ask_llm(
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
            brief = await _fetch_url(ref_url)
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



async def _ai_edit_chat(history: list, site_context: str = "") -> dict:
    # handle ai edit chat turn
    system_text = EDIT_CHAT_SYSTEM
    if site_context:
        system_text += f"\n\n=== ТЕКУЩИЙ КОНТЕНТ САЙТА ===\n{site_context}"
    resp = await _ask_llm(
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



async def _ai_chat(history: list) -> dict:
    # generate onboarding response
    resp = await _ask_llm(
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


