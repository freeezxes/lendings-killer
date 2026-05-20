import hashlib
import json
import os
import re

import anthropic

import db
import analytics_service
import marketing_credit_service


BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
PRICE_INPUT = 1.00
PRICE_OUTPUT = 5.00
GENERATION_MIN_CREDITS = 10

CONTENT_FIELDS = (
    "ad_copy",
    "captions",
    "hashtags",
    "hooks",
    "banner_prompts",
    "reels_ideas",
    "campaign_structures",
    "seo_texts",
    "google_ads",
)

SYSTEM_PROMPT = """Ты — AI marketing manager для малого бизнеса в Казахстане.

Работай только с предоставленными данными. Не выдумывай метрики, отзывы, результаты, гарантии, адреса или опыт.
Если данных мало — честно укажи это в warnings.

Верни строго валидный JSON без markdown:
{
  "summary": "короткое резюме стратегии",
  "warnings": ["честные ограничения"],
  "content": {
    "ad_copy": [{"platform":"instagram","headline":"...","text":"...","cta":"..."}],
    "captions": ["..."],
    "hashtags": ["#..."],
    "hooks": ["..."],
    "banner_prompts": ["..."],
    "reels_ideas": [{"title":"...","script":"..."}],
    "campaign_structures": [{"platform":"...","objective":"...","audience":"...","budget_note":"..."}],
    "seo_texts": [{"title":"...","description":"..."}],
    "google_ads": [{"headline":"...","description":"..."}]
  }
}

Контент должен быть на русском языке, простым для владельца малого бизнеса и готовым к использованию."""


ai_client = anthropic.AnthropicBedrock(
    aws_region=os.environ.get("AWS_REGION", "us-east-1"),
)


def _json_load(value, fallback=None):
    # safe json
    if fallback is None:
        fallback = {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _calc_cost(inp: int, out: int, cr: int = 0, cc: int = 0) -> float:
    # calc cost
    return (
        inp * PRICE_INPUT +
        out * PRICE_OUTPUT +
        cr * PRICE_INPUT * 0.1 +
        cc * PRICE_INPUT * 1.25
    ) / 1_000_000


def _credits_from_usage(inp: int, out: int) -> int:
    # marketing credit usage
    return max(GENERATION_MIN_CREDITS, round((int(inp or 0) + int(out or 0)) / 1_000))


def _safe_int(value) -> int | None:
    # safe int
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _business_context(site: dict | None) -> dict:
    # business context
    data = _json_load((site or {}).get("data"), {})
    return {
        "site_id": (site or {}).get("id"),
        "title": (site or {}).get("title") or data.get("name") or "",
        "slug": (site or {}).get("slug") or "",
        "name": data.get("name") or "",
        "services": data.get("services") or "",
        "city_contact": data.get("city") or "",
        "style": data.get("vibe") or "",
    }


def _validate_campaign_requirements(payload: dict) -> tuple[list[str], dict]:
    # validate campaign requirements
    fields = {
        "goal": str(payload.get("goal") or "").strip(),
        "target_audience": str(payload.get("target_audience") or "").strip(),
        "location": str(payload.get("location") or "").strip(),
        "budget": str(payload.get("budget") or payload.get("budget_credits") or "").strip(),
        "platforms": payload.get("platforms") or [],
        "objective": str(payload.get("objective") or "").strip(),
    }
    if isinstance(fields["platforms"], str):
        fields["platforms"] = [p.strip() for p in fields["platforms"].split(",") if p.strip()]
    missing = [key for key, value in fields.items() if not value]
    return missing, fields


def assistant_message(payload: dict) -> dict:
    # marketing assistant message
    missing, fields = _validate_campaign_requirements(payload)
    questions = {
        "goal": "Какая главная цель: больше записей, узнаваемость, повторные клиенты или запуск новой услуги?",
        "target_audience": "Кого хотим привлечь? Например: женщины 20-35 рядом с салоном или родители школьников.",
        "location": "В каком городе или районе показывать продвижение?",
        "budget": "Какой бюджет в маркетинговых кредитах готовы выделить на тест?",
        "platforms": "Где продвигаемся: Instagram, TikTok, Google или SEO?",
        "objective": "Что считаем успехом кампании: клики в WhatsApp, заявки, охват или переходы на сайт?",
    }
    return {
        "ok": True,
        "ready": not missing,
        "missing_fields": missing,
        "collected": fields,
        "reply": "Данных достаточно. Могу подготовить креативы и структуру кампании." if not missing else questions[missing[0]],
    }


def _build_user_prompt(site: dict | None, payload: dict, analytics: dict) -> str:
    # build prompt
    missing, requirements = _validate_campaign_requirements(payload)
    return json.dumps({
        "business_context": _business_context(site),
        "campaign_requirements": requirements,
        "requested_content_type": payload.get("content_type") or "campaign_pack",
        "format_constraints": {
            "language": "ru",
            "avoid_fake_metrics": True,
            "missing_fields": missing,
            "platforms": requirements.get("platforms"),
        },
        "analytics_context": analytics,
        "output_contract": {
            "summary": "string",
            "warnings": "array[string]",
            "content": {field: "array" for field in CONTENT_FIELDS},
        },
    }, ensure_ascii=False, indent=2)


def _parse_ai_json(raw: str) -> dict:
    # parse ai json
    text = (raw or "").strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def validate_output(result: dict) -> dict:
    # validate output contract
    if not isinstance(result, dict):
        raise ValueError("AI returned invalid JSON")
    content = result.get("content")
    if not isinstance(content, dict):
        raise ValueError("AI response missing content")
    for field in CONTENT_FIELDS:
        value = content.get(field)
        if value is None:
            content[field] = []
        elif not isinstance(value, list):
            raise ValueError(f"AI response field {field} must be a list")
    warnings = result.get("warnings")
    if warnings is None:
        result["warnings"] = []
    elif not isinstance(warnings, list):
        raise ValueError("AI response warnings must be a list")
    result["summary"] = str(result.get("summary") or "").strip()[:600]
    return result


def _insert_content(user_id: int, site_id: int | None, campaign_id: int | None,
                    content_type: str, platform: str, input_json: dict,
                    output_json: dict, prompt_hash: str,
                    parent_content_id: int | None = None) -> int:
    # insert content
    with db.get_conn() as c:
        version_no = 1
        if parent_content_id:
            row = c.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM marketing_generated_content WHERE parent_content_id=? OR id=?",
                (parent_content_id, parent_content_id),
            ).fetchone()
            version_no = int(row[0] or 1)
        cur = c.execute(
            """INSERT INTO marketing_generated_content
               (user_id, site_id, campaign_id, content_type, platform, prompt_hash,
                input_json, output_json, version_no, status, parent_content_id,
                credits_spent, created, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                user_id,
                site_id,
                campaign_id,
                content_type,
                platform,
                prompt_hash,
                json.dumps(input_json, ensure_ascii=False),
                json.dumps(output_json, ensure_ascii=False),
                version_no,
                "draft",
                parent_content_id,
                0,
            ),
        )
        return cur.lastrowid


def generate_content(user_id: int, payload: dict, site: dict | None = None,
                     parent_content_id: int | None = None) -> dict:
    # generate marketing content
    if marketing_credit_service.balance(user_id) < GENERATION_MIN_CREDITS:
        return {
            "ok": False,
            "error": "insufficient_marketing_credits",
            "message": f"Нужно минимум {GENERATION_MIN_CREDITS} маркетинговых кредитов.",
        }
    missing, _ = _validate_campaign_requirements(payload)
    if missing:
        return assistant_message(payload)

    site_id = int((site or {}).get("id") or payload.get("site_id") or 0) or None
    analytics = analytics_service.aggregate_user(user_id)
    user_prompt = _build_user_prompt(site, payload, analytics)
    prompt_hash = hashlib.sha256(user_prompt.encode()).hexdigest()
    try:
        resp = ai_client.messages.create(
            model=BEDROCK_MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text
        output = validate_output(_parse_ai_json(raw))
    except Exception as exc:
        return {"ok": False, "error": "ai_generation_failed", "message": f"AI не вернул валидный ответ: {exc}"}

    usage = resp.usage
    inp = int(usage.input_tokens or 0)
    out = int(usage.output_tokens or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    credits = _credits_from_usage(inp, out)
    cost = _calc_cost(inp, out, cache_read, cache_create)

    campaign_id = _safe_int(payload.get("campaign_id"))
    content_id = _insert_content(
        user_id,
        site_id,
        campaign_id,
        str(payload.get("content_type") or "campaign_pack"),
        ",".join(payload.get("platforms") or []) if isinstance(payload.get("platforms"), list) else str(payload.get("platforms") or ""),
        payload,
        output,
        prompt_hash,
        parent_content_id,
    )
    spent = marketing_credit_service.deduct_credits(
        user_id,
        credits,
        "marketing_content_generate" if not parent_content_id else "marketing_content_regenerate",
        site_id=site_id,
        campaign_id=campaign_id,
        content_id=content_id,
        claude_in=inp,
        claude_out=out,
        cache_read=cache_read,
        cost_usd=cost,
    )
    if not spent.get("ok"):
        return spent
    with db.get_conn() as c:
        c.execute(
            "UPDATE marketing_generated_content SET credits_spent=?, updated=datetime('now') WHERE id=?",
            (credits, content_id),
        )
    return {
        "ok": True,
        "content_id": content_id,
        "content": output,
        "credits_spent": credits,
        "promo_credits": spent.get("balance"),
    }


def list_content(user_id: int, limit: int = 30) -> list[dict]:
    # list generated content
    with db.get_conn() as c:
        rows = c.execute(
            """SELECT mgc.*, s.title as site_title, mc.campaign_name
               FROM marketing_generated_content mgc
               LEFT JOIN sites s ON s.id=mgc.site_id
               LEFT JOIN marketing_campaigns mc ON mc.id=mgc.campaign_id
               WHERE mgc.user_id=?
               ORDER BY datetime(mgc.created) DESC, mgc.id DESC
               LIMIT ?""",
            (user_id, int(limit or 30)),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["input"] = _json_load(item.get("input_json"))
        item["output"] = _json_load(item.get("output_json"))
        result.append(item)
    return result


def save_content(user_id: int, content_id: int) -> dict:
    # save content draft
    with db.get_conn() as c:
        cur = c.execute(
            """UPDATE marketing_generated_content
               SET status='saved', updated=datetime('now')
               WHERE id=? AND user_id=?""",
            (content_id, user_id),
        )
    if cur.rowcount != 1:
        return {"ok": False, "error": "content_not_found", "message": "Контент не найден."}
    return {"ok": True}


def regenerate_content(user_id: int, content_id: int) -> dict:
    # regenerate content
    with db.get_conn() as c:
        row = c.execute(
            "SELECT * FROM marketing_generated_content WHERE id=? AND user_id=?",
            (content_id, user_id),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "content_not_found", "message": "Контент не найден."}
    item = dict(row)
    payload = _json_load(item.get("input_json"))
    site = db.get_user_site_by_id(user_id, item.get("site_id")) if item.get("site_id") else None
    return generate_content(user_id, payload, site, parent_content_id=content_id)
