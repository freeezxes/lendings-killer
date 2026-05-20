import json
from datetime import datetime

import db
import analytics_service
import marketing_credit_service


ACTIVE_STATUSES = {"active", "paused"}
VALID_STATUSES = {"draft", "active", "paused", "archived", "completed", "failed"}
VALID_PLATFORMS = {"instagram", "facebook", "tiktok", "google", "seo", "internal"}
IMPROVE_COST = 20


def _safe_json(value, fallback=None):
    # safe json
    if fallback is None:
        fallback = {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _owned_site(user_id: int, site_id: int | None) -> dict | None:
    # owned site
    if not site_id:
        return None
    return db.get_user_site_by_id(user_id, int(site_id))


def _present(row: dict) -> dict:
    # present campaign
    budget = int(row.get("budget_credits") or 0)
    stats = latest_stats(int(row["id"]))
    row["brief"] = _safe_json(row.get("brief_json"))
    row["content"] = _safe_json(row.get("content_json"))
    row["stats"] = stats
    row["ctr"] = stats.get("ctr") or 0
    row["cpc"] = stats.get("cpc") or 0
    row["conversion_rate"] = stats.get("conversion_rate") or 0
    row["budget_left"] = max(0, budget - int(stats.get("spend_credits") or 0))
    row["status_label"] = {
        "draft": "Черновик",
        "active": "Активна",
        "paused": "На паузе",
        "archived": "В архиве",
        "completed": "Завершена",
        "failed": "Ошибка",
    }.get(row.get("status"), row.get("status"))
    return row


def _calculate_rates(values: dict) -> dict:
    # calculate rates
    impressions = int(values.get("impressions") or 0)
    clicks = int(values.get("clicks") or 0)
    leads = int(values.get("leads") or 0)
    spend = int(values.get("spend_amount_kzt") or 0)
    values["ctr"] = round((clicks / impressions) * 100, 2) if impressions else 0
    values["cpc"] = round(spend / clicks, 2) if clicks else 0
    values["conversion_rate"] = round((leads / clicks) * 100, 2) if clicks else 0
    return values


def latest_stats(campaign_id: int) -> dict:
    # latest campaign stats
    with db.get_conn() as c:
        row = c.execute(
            """SELECT * FROM marketing_campaign_stats
               WHERE campaign_id=?
               ORDER BY snapshot_at DESC, id DESC LIMIT 1""",
            (campaign_id,),
        ).fetchone()
    if not row:
        return {
            "impressions": 0,
            "clicks": 0,
            "leads": 0,
            "conversions": 0,
            "spend_credits": 0,
            "spend_amount_kzt": 0,
            "ctr": 0,
            "cpc": 0,
            "conversion_rate": 0,
        }
    return dict(row)


def list_campaigns(user_id: int, status: str | None = None, limit: int = 50) -> list[dict]:
    # list campaigns
    params = [user_id]
    where = "WHERE user_id=?"
    if status:
        where += " AND status=?"
        params.append(status)
    params.append(int(limit or 50))
    with db.get_conn() as c:
        rows = c.execute(
            f"""SELECT * FROM marketing_campaigns
                {where}
                ORDER BY datetime(updated) DESC, id DESC LIMIT ?""",
            params,
        ).fetchall()
    return [_present(dict(r)) for r in rows]


def get_campaign(user_id: int, campaign_id: int) -> dict | None:
    # get campaign
    with db.get_conn() as c:
        row = c.execute(
            "SELECT * FROM marketing_campaigns WHERE id=? AND user_id=?",
            (campaign_id, user_id),
        ).fetchone()
    return _present(dict(row)) if row else None


def create_campaign(user_id: int, payload: dict) -> dict:
    # create campaign
    site = _owned_site(user_id, payload.get("site_id"))
    if not site:
        return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
    platform = str(payload.get("platform") or "instagram").lower()
    if platform not in VALID_PLATFORMS:
        return {"ok": False, "error": "invalid_platform", "message": "Платформа не поддерживается."}
    status = str(payload.get("status") or "active").lower()
    if status not in VALID_STATUSES:
        status = "draft"
    try:
        budget = max(0, int(payload.get("budget_credits") or payload.get("budget") or 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_budget", "message": "Введите корректный бюджет."}
    if status == "active" and budget <= 0:
        return {"ok": False, "error": "invalid_budget", "message": "Для запуска нужен бюджет в маркетинговых кредитах."}

    brief = {
        "goal": str(payload.get("goal") or "").strip(),
        "target_audience": str(payload.get("target_audience") or "").strip(),
        "location": str(payload.get("location") or site.get("data", {}).get("city") or "").strip(),
        "objective": str(payload.get("objective") or "").strip(),
        "platforms": payload.get("platforms") or [platform],
    }
    with db.get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        active = c.execute(
            """SELECT id FROM marketing_campaigns
               WHERE user_id=? AND site_id=? AND status='active' LIMIT 1""",
            (user_id, site["id"]),
        ).fetchone()
        if active and status == "active":
            return {"ok": False, "error": "active_campaign_exists", "message": "Для сайта уже есть активная кампания."}
        balance_after = None
        if status == "active":
            updated = c.execute(
                """UPDATE users
                   SET promo_credits=promo_credits-?
                   WHERE id=? AND promo_credits>=?""",
                (budget, user_id, budget),
            )
            if updated.rowcount != 1:
                return {
                    "ok": False,
                    "error": "insufficient_marketing_credits",
                    "message": "Недостаточно маркетинговых кредитов.",
                }
            balance_after = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        cur = c.execute(
            """INSERT INTO marketing_campaigns
               (user_id, site_id, platform, status, objective, budget_credits,
                budget_amount_kzt, target_audience, location, campaign_name,
                auto_optimize, source, brief_json, content_json, created, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                user_id,
                site["id"],
                platform,
                status,
                brief["objective"],
                budget,
                budget * 10,
                brief["target_audience"],
                brief["location"],
                str(payload.get("campaign_name") or f"{site.get('title') or site.get('slug')} - {platform}").strip(),
                1 if payload.get("auto_optimize") else 0,
                "internal",
                json.dumps(brief, ensure_ascii=False),
                json.dumps(payload.get("content") or {}, ensure_ascii=False),
            ),
        )
        campaign_id = cur.lastrowid
        if status == "active":
            legacy = c.execute(
                """INSERT INTO promo_credit_log
                   (user_id, site_id, campaign_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,?,?,datetime('now'))""",
                (user_id, site["id"], campaign_id, -budget, "marketing_campaign_launch", balance_after),
            )
            c.execute(
                """INSERT INTO marketing_credit_logs
                   (user_id, site_id, campaign_id, delta, reason, balance_after,
                    legacy_promo_credit_log_id, created)
                   VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (user_id, site["id"], campaign_id, -budget, "marketing_campaign_launch", balance_after, legacy.lastrowid),
            )
        c.execute(
            """INSERT INTO marketing_campaign_stats
               (campaign_id, impressions, clicks, leads, conversions,
                spend_credits, spend_amount_kzt, source_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                campaign_id,
                0,
                0,
                0,
                0,
                budget if status == "active" else 0,
                budget * 10 if status == "active" else 0,
                json.dumps({"source": "internal_v1"}, ensure_ascii=False),
            ),
        )
    return {"ok": True, "campaign": get_campaign(user_id, campaign_id)}


def update_status(user_id: int, campaign_id: int, status: str) -> dict:
    # update campaign status
    if status not in {"active", "paused", "archived"}:
        return {"ok": False, "error": "invalid_status", "message": "Статус не поддерживается."}
    campaign = get_campaign(user_id, campaign_id)
    if not campaign:
        return {"ok": False, "error": "campaign_not_found", "message": "Кампания не найдена."}
    archived_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") if status == "archived" else None
    with db.get_conn() as c:
        if status == "active":
            active = c.execute(
                """SELECT id FROM marketing_campaigns
                   WHERE user_id=? AND site_id=? AND status='active' AND id<>?
                   LIMIT 1""",
                (user_id, campaign.get("site_id"), campaign_id),
            ).fetchone()
            if active:
                return {"ok": False, "error": "active_campaign_exists", "message": "Для сайта уже есть активная кампания."}
        c.execute(
            """UPDATE marketing_campaigns
               SET status=?, archived_at=COALESCE(?, archived_at), updated=datetime('now')
               WHERE id=? AND user_id=?""",
            (status, archived_at, campaign_id, user_id),
        )
    return {"ok": True, "campaign": get_campaign(user_id, campaign_id)}


def duplicate_campaign(user_id: int, campaign_id: int) -> dict:
    # duplicate campaign
    campaign = get_campaign(user_id, campaign_id)
    if not campaign:
        return {"ok": False, "error": "campaign_not_found", "message": "Кампания не найдена."}
    with db.get_conn() as c:
        cur = c.execute(
            """INSERT INTO marketing_campaigns
               (user_id, site_id, platform, status, objective, budget_credits,
                budget_amount_kzt, target_audience, location, campaign_name,
                auto_optimize, source, brief_json, content_json, created, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                user_id,
                campaign.get("site_id"),
                campaign.get("platform"),
                "draft",
                campaign.get("objective"),
                campaign.get("budget_credits") or 0,
                campaign.get("budget_amount_kzt") or 0,
                campaign.get("target_audience") or "",
                campaign.get("location") or "",
                f"{campaign.get('campaign_name') or 'Campaign'} copy",
                int(campaign.get("auto_optimize") or 0),
                "duplicate",
                json.dumps(campaign.get("brief") or {}, ensure_ascii=False),
                json.dumps(campaign.get("content") or {}, ensure_ascii=False),
            ),
        )
    return {"ok": True, "campaign": get_campaign(user_id, cur.lastrowid)}


def improve_campaign(user_id: int, campaign_id: int) -> dict:
    # improve campaign
    campaign = get_campaign(user_id, campaign_id)
    if not campaign:
        return {"ok": False, "error": "campaign_not_found", "message": "Кампания не найдена."}
    spent = marketing_credit_service.deduct_credits(
        user_id,
        IMPROVE_COST,
        "marketing_campaign_improve",
        site_id=campaign.get("site_id"),
        campaign_id=campaign_id,
    )
    if not spent.get("ok"):
        return spent
    metrics = analytics_service.aggregate_user(user_id)
    suggestions = {
        "type": "campaign_improvement",
        "insights": metrics.get("insights") or [],
        "actions": [
            "Сузить аудиторию вокруг самого сильного источника трафика.",
            "Обновить первый хук объявления под конкретную услугу.",
            "Проверить, видна ли кнопка записи на первом экране сайта.",
        ],
        "note": "Предложения основаны только на доступной аналитике lendings.kz.",
    }
    content_id = _insert_content(
        user_id,
        campaign.get("site_id"),
        campaign_id,
        "optimization",
        campaign.get("platform"),
        {"campaign_id": campaign_id, "metrics": metrics},
        suggestions,
        IMPROVE_COST,
    )
    return {
        "ok": True,
        "content_id": content_id,
        "suggestions": suggestions,
        "promo_credits": spent.get("balance"),
    }


def _insert_content(user_id: int, site_id: int | None, campaign_id: int | None,
                    content_type: str, platform: str | None, input_json: dict,
                    output_json: dict, credits_spent: int = 0,
                    parent_content_id: int | None = None) -> int:
    # insert generated content
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
                platform or "",
                "",
                json.dumps(input_json, ensure_ascii=False),
                json.dumps(output_json, ensure_ascii=False),
                version_no,
                "draft",
                parent_content_id,
                credits_spent,
            ),
        )
        return cur.lastrowid
