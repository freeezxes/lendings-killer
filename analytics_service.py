import json
from datetime import datetime, timedelta
from urllib.parse import urlparse

import db


CONTACT_EVENTS = {"whatsapp_click", "telegram_click", "phone_click"}
CLICK_EVENTS = CONTACT_EVENTS | {"cta_click", "service_click", "instagram_click"}


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


def _pct(part: int, total: int) -> float:
    # percent
    return round((part / total) * 100, 2) if total else 0.0


def _source(payload: dict) -> str:
    # traffic source
    ref = str(payload.get("referrer") or "").strip()
    if not ref:
        return "Direct"
    host = urlparse(ref).netloc.lower()
    if "instagram" in host:
        return "Instagram"
    if "google" in host:
        return "Google"
    if "tiktok" in host:
        return "TikTok"
    if "facebook" in host or "fb." in host:
        return "Facebook"
    if "t.me" in host or "telegram" in host:
        return "Telegram"
    return host or "Referral"


def _empty_metrics() -> dict:
    # empty metrics
    return {
        "visitors": 0,
        "clicks": 0,
        "leads": 0,
        "conversions": 0,
        "conversion_rate": 0,
        "traffic_sources": [],
        "funnel": [
            {"label": "Visitors", "value": 0},
            {"label": "Clicks", "value": 0},
            {"label": "Leads", "value": 0},
        ],
        "timeline": [],
        "insights": [{
            "level": "neutral",
            "title": "Нет данных",
            "body": "Инсайты появятся после первых посещений и кликов.",
        }],
    }


def aggregate_user(user_id: int, days: int = 30) -> dict:
    # aggregate marketing analytics
    since = (datetime.utcnow() - timedelta(days=int(days or 30))).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as c:
        events = c.execute(
            """SELECT ae.event_type, ae.payload_json, ae.created, s.user_id, s.id as site_id
               FROM analytics_events ae
               JOIN sites s ON s.id=ae.site_id
               WHERE s.user_id=? AND ae.created>=?
               ORDER BY ae.created ASC""",
            (user_id, since),
        ).fetchall()
        stats = c.execute(
            """SELECT COALESCE(SUM(mcs.impressions),0) as impressions,
                      COALESCE(SUM(mcs.clicks),0) as campaign_clicks,
                      COALESCE(SUM(mcs.leads),0) as campaign_leads,
                      COALESCE(SUM(mcs.conversions),0) as conversions,
                      COALESCE(SUM(mcs.spend_credits),0) as spend_credits,
                      COALESCE(SUM(mcs.spend_amount_kzt),0) as spend_amount_kzt
               FROM marketing_campaign_stats mcs
               JOIN marketing_campaigns mc ON mc.id=mcs.campaign_id
               WHERE mc.user_id=?""",
            (user_id,),
        ).fetchone()

    visitors = 0
    clicks = 0
    leads = 0
    source_counts: dict[str, int] = {}
    by_day: dict[str, dict] = {}

    for event in events:
        event_type = event["event_type"]
        day = str(event["created"] or "")[:10]
        by_day.setdefault(day, {"date": day, "visitors": 0, "clicks": 0, "leads": 0})
        if event_type == "page_view":
            visitors += 1
            by_day[day]["visitors"] += 1
            src = _source(_safe_json(event["payload_json"]))
            source_counts[src] = source_counts.get(src, 0) + 1
        if event_type in CLICK_EVENTS:
            clicks += 1
            by_day[day]["clicks"] += 1
        if event_type in CONTACT_EVENTS:
            leads += 1
            by_day[day]["leads"] += 1

    campaign_clicks = int(stats["campaign_clicks"] or 0) if stats else 0
    campaign_leads = int(stats["campaign_leads"] or 0) if stats else 0
    conversions = int(stats["conversions"] or 0) if stats else 0
    clicks += campaign_clicks
    leads += campaign_leads

    traffic_sources = [
        {"source": source, "visitors": count, "share": _pct(count, visitors)}
        for source, count in sorted(source_counts.items(), key=lambda x: x[1], reverse=True)
    ]
    metrics = {
        "visitors": visitors,
        "clicks": clicks,
        "leads": leads,
        "conversions": conversions,
        "conversion_rate": _pct(leads, visitors),
        "traffic_sources": traffic_sources,
        "funnel": [
            {"label": "Visitors", "value": visitors},
            {"label": "Clicks", "value": clicks},
            {"label": "Leads", "value": leads},
        ],
        "timeline": list(by_day.values())[-14:],
        "campaign_impressions": int(stats["impressions"] or 0) if stats else 0,
        "spend_credits": int(stats["spend_credits"] or 0) if stats else 0,
        "spend_amount_kzt": int(stats["spend_amount_kzt"] or 0) if stats else 0,
    }
    metrics["insights"] = build_insights(metrics)
    return metrics if events or campaign_clicks or campaign_leads else _empty_metrics()


def build_insights(metrics: dict) -> list[dict]:
    # build realistic insights
    visitors = int(metrics.get("visitors") or 0)
    clicks = int(metrics.get("clicks") or 0)
    leads = int(metrics.get("leads") or 0)
    sources = metrics.get("traffic_sources") or []
    if visitors < 10:
        return [{
            "level": "neutral",
            "title": "Нет данных",
            "body": "Нужно больше посещений и кликов для инсайтов.",
        }]

    insights = []
    if sources:
        top = sources[0]
        insights.append({
            "level": "good",
            "title": f"{top['source']} приводит больше всего трафика",
            "body": f"Источник даёт {top['share']}% посещений. Усильте контент и ссылки там, где уже есть спрос.",
        })
    if clicks and not leads:
        insights.append({
            "level": "warn",
            "title": "Клики есть, заявок пока нет",
            "body": "Проверьте заметность WhatsApp/Telegram кнопок и конкретность предложения в первом экране.",
        })
    elif leads:
        insights.append({
            "level": "good",
            "title": "Контактные клики уже есть",
            "body": f"Сайт получил {leads} целевых переходов. Следующий шаг - тестировать новые офферы и креативы.",
        })
    if _pct(clicks, visitors) < 3:
        insights.append({
            "level": "warn",
            "title": "CTR сайта низкий",
            "body": "Добавьте более конкретный призыв к записи и вынесите популярную услугу выше.",
        })
    return insights[:3]


def create_snapshot(user_id: int, site_id: int | None = None, days: int = 30) -> dict:
    # create analytics snapshot
    metrics = aggregate_user(user_id, days)
    now = datetime.utcnow()
    with db.get_conn() as c:
        c.execute(
            """INSERT INTO marketing_analytics_snapshots
               (user_id, site_id, period_start, period_end, visitors, clicks,
                leads, conversion_rate, traffic_sources_json, funnel_json,
                insights_json, created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (
                user_id,
                site_id,
                (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d %H:%M:%S"),
                metrics["visitors"],
                metrics["clicks"],
                metrics["leads"],
                metrics["conversion_rate"],
                json.dumps(metrics["traffic_sources"], ensure_ascii=False),
                json.dumps(metrics["funnel"], ensure_ascii=False),
                json.dumps(metrics["insights"], ensure_ascii=False),
            ),
        )
    return metrics
