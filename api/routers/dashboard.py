from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import re
import json

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    import main
    return await main.dashboard_view(request, "overview")

@router.get("/sites", response_class=HTMLResponse)
async def dashboard_sites(request: Request):
    return RedirectResponse("/dashboard", status_code=302)

@router.get("/sites/{site_id}", response_class=HTMLResponse)
async def dashboard_site_workspace(site_id: int, request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    context = main.services.build_site_workspace_context(user, site_id)
    if not context:
        return RedirectResponse("/dashboard?missing=site", status_code=302)
    context["verification_notice"] = main._verification_notice(request, context["user"])
    context["dashboard_view"] = "site"
    return main.templates.TemplateResponse(request, "dashboard.html", context)

@router.get("/billing", response_class=HTMLResponse)
async def dashboard_billing(request: Request):
    import main
    slot_pkg = next(p for p in main.PAYMENT_PACKAGES if p["type"] == "slot")
    credit_pkgs = [p for p in main.PAYMENT_PACKAGES if p["type"] == "credits"]
    return await main.dashboard_view(request, "billing", slot_pkg=slot_pkg, credit_pkgs=credit_pkgs)

@router.get("/create", response_class=HTMLResponse)
async def dashboard_create(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return RedirectResponse("/auth?next=/dashboard/create", status_code=302)
    
    if blocked := main._require_paid(user):
        return blocked

    edit_slug = request.query_params.get("edit", "").strip()
    if not edit_slug:
        sites = main.db.get_user_sites(user["id"])
        if len(sites) >= user.get("site_slots", 0):
            return RedirectResponse("/payment?reason=no_slots", status_code=302)

    edit_site = None
    if edit_slug:
        edit_slug = re.sub(r"[^a-zA-Z0-9_-]", "", edit_slug)
        site = main.db.get_site_by_slug(edit_slug)
        if site and site["user_id"] == user["id"]:
            site = main.services.SupportService.refresh_site(site["id"]) or site
            if not main.services.is_support_operational(site.get("support_status")):
                return RedirectResponse("/dashboard?support=inactive", status_code=302)
            site_data = site.get("data") or {}
            edit_site = {
                "slug": site["slug"],
                "title": site["title"],
                "history": json.dumps(site_data.get("chat_history", []), ensure_ascii=False),
            }
    return await main.dashboard_view(
        request,
        "create",
        edit_site=edit_site,
        onboarding=main.services.OnboardingService.current(user["id"]) if not edit_site else None,
    )
