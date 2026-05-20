import analytics_service
import campaign_service
import marketing_ai_service
import marketing_credit_service
import services
from domain import PROMO_CREDIT_TENGE, PROMO_MIN_PURCHASE


def overview(user: dict) -> dict:
    # marketing overview
    metrics = analytics_service.aggregate_user(user["id"])
    campaigns = campaign_service.list_campaigns(user["id"], limit=100)
    active = [c for c in campaigns if c.get("status") == "active"]
    spend_credits = sum(int((c.get("stats") or {}).get("spend_credits") or 0) for c in campaigns)
    spend_amount = sum(int((c.get("stats") or {}).get("spend_amount_kzt") or 0) for c in campaigns)
    return {
        "visitors": metrics.get("visitors") or 0,
        "clicks": metrics.get("clicks") or 0,
        "leads": metrics.get("leads") or 0,
        "conversions": metrics.get("conversions") or 0,
        "conversion_rate": metrics.get("conversion_rate") or 0,
        "active_campaigns": len(active),
        "total_campaigns": len(campaigns),
        "spend_credits": spend_credits,
        "spend_amount_kzt": spend_amount,
        "roi": None,
        "marketing_credits": int(user.get("promo_credits") or 0),
        "timeline": metrics.get("timeline") or [],
        "traffic_sources": metrics.get("traffic_sources") or [],
        "funnel": metrics.get("funnel") or [],
        "insights": metrics.get("insights") or [],
    }


def dashboard_context(user: dict) -> dict:
    # marketing dashboard context
    base = services.build_dashboard_context(user)
    fresh_user = base["user"]
    campaigns = campaign_service.list_campaigns(fresh_user["id"], limit=50)
    content = marketing_ai_service.list_content(fresh_user["id"], limit=12)
    context = {
        **base,
        "marketing_overview": overview(fresh_user),
        "marketing_campaigns": campaigns,
        "marketing_content": content,
        "marketing_credit_logs": marketing_credit_service.logs(fresh_user["id"], limit=12),
        "marketing_packages": [
            {"credits": PROMO_MIN_PURCHASE, "amount": PROMO_MIN_PURCHASE * PROMO_CREDIT_TENGE},
            {"credits": PROMO_MIN_PURCHASE * 3, "amount": PROMO_MIN_PURCHASE * 3 * PROMO_CREDIT_TENGE},
            {"credits": PROMO_MIN_PURCHASE * 10, "amount": PROMO_MIN_PURCHASE * 10 * PROMO_CREDIT_TENGE},
        ],
    }
    context["marketing_stats"] = {
        "total_campaigns": context["marketing_overview"]["total_campaigns"],
        "total_spend": context["marketing_overview"]["spend_amount_kzt"],
        "total_clicks": context["marketing_overview"]["clicks"],
        "total_views": context["marketing_overview"]["visitors"],
    }
    return context
