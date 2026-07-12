from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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
    from core.database import AsyncSessionLocal
    from repositories.site_repo import site_repo
    user = main._require_auth(request)
    if not user:
        return RedirectResponse("/auth?next=/dashboard/create", status_code=302)
    
    if blocked := main._require_paid(user):
        return blocked

    edit_slug = request.query_params.get("edit", "").strip()
    async with AsyncSessionLocal() as session:
        if not edit_slug:
            sites = await site_repo.get_multi_by_user(session, user.id)
            if len(sites) >= (user.site_slots or 0):
                return RedirectResponse("/payment?reason=no_slots", status_code=302)

        edit_site = None
        if edit_slug:
            edit_slug = re.sub(r"[^a-zA-Z0-9_-]", "", edit_slug)
            site = await site_repo.get_by_slug(session, edit_slug)
            if site and site.user_id == user.id:
                site = await main.services.SupportService.refresh_site(site.id) or site
                if not main.services.is_support_operational(site.support_status):
                    return RedirectResponse("/dashboard?support=inactive", status_code=302)
                site_data = site.data or {}
                edit_site = {
                    "slug": site.slug,
                    "title": site.title,
                    "history": json.dumps(site_data.get("chat_history", []), ensure_ascii=False),
                }
        return await main.dashboard_view(
            request,
            "create",
            edit_site=edit_site,
            onboarding=await main.services.OnboardingService.current(user.id) if not edit_site else None,
        )@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    import main
    from core.database import AsyncSessionLocal
    from repositories.site_repo import site_repo
    from repositories.log_repo import dev_credit_log_repo, promo_credit_log_repo
    user = main._require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    async with AsyncSessionLocal() as session:
        log = await dev_credit_log_repo.get_multi_by_user(session, user.id)
        promo_log = await promo_credit_log_repo.get_multi_by_user(session, user.id)
        sites = await site_repo.get_multi_by_user(session, user.id)
        sites_count = len(sites)
    csrf_token = await main.auth_services.CsrfService.generate()
    response = main.templates.TemplateResponse(request, "profile.html", {
        "user": user,
        "log": log,
        "promo_log": promo_log,
        "sites_count": sites_count,
        "verification_notice": main._verification_notice(request, user),
        "csrf_token": csrf_token,
    })
    main._set_auth_csrf_cookie(response, request, csrf_token)
    return response@router.post("/profile/update")
async def profile_update(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    avatar_url: str = Form(""),
    csrf_token: str = Form(""),
):
    import main
    from core.database import AsyncSessionLocal
    from repositories.user_repo import user_repo
    user = main._require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    try:
        await main._verify_auth_csrf(request, csrf_token)
        safe_name = main.auth_services.validate_name(name)
    except main.auth_services.AuthError:
        return RedirectResponse("/dashboard/profile?email_error=verification_failed", status_code=302)
    
    async with AsyncSessionLocal() as session:
        db_user = await user_repo.get(session, user.id)
        if db_user:
            db_user.name = safe_name
            if avatar_url:
                db_user.avatar_url = avatar_url
            session.add(db_user)
            await session.commit()
            
    try:
        new_email = main.auth_services.validate_email(email) if (email or "").strip() else ""
    except main.auth_services.AuthError:
        return RedirectResponse("/dashboard/profile?email_error=invalid_email", status_code=302)
    current_email = main.auth_services.normalize_email(user.email)
    
    if new_email and new_email != current_email:
        async with AsyncSessionLocal() as session:
            db_user = await user_repo.get(session, user.id)
            if db_user:
                db_user.email = new_email
                db_user.email_verified = 0
                db_user.email_verify_token = None
                db_user.email_verify_expires = None
                db_user.verification_sent_at = None
                session.add(db_user)
                try:
                    await session.commit()
                    updated = db_user
                except Exception:
                    return RedirectResponse("/dashboard/profile?email_error=account_conflict", status_code=302)
            else:
                updated = None
                
        if updated:
            result = await main._prepare_and_send_verification(request, updated, rate_limit=False)
            if result.get("ok"):
                return RedirectResponse("/dashboard/profile?email_success=verification_sent", status_code=302)
            return RedirectResponse(f"/dashboard/profile?email_error={result.get('error', 'verification_failed')}", status_code=302)

    return RedirectResponse("/dashboard/profile", status_code=302)@router.post("/profile/update-password")
async def profile_update_password(
    request: Request,
    password: str = Form(...),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    import main
    from core.database import AsyncSessionLocal
    from repositories.user_repo import user_repo
    import bcrypt
    user = main._require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    try:
        await main._verify_auth_csrf(request, csrf_token)
        main.auth_services.validate_password(password, confirm_password, email=user.email, name=user.name)
    except main.auth_services.AuthError as e:
        return RedirectResponse(f"/dashboard/profile?password_error={e.code}", status_code=302)

    async with AsyncSessionLocal() as session:
        db_user = await user_repo.get(session, user.id)
        if db_user:
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
            db_user.password = hashed
            db_user.password_hash = hashed
            if db_user.auth_provider == 'google':
                db_user.auth_provider = 'hybrid'
            else:
                db_user.auth_provider = db_user.auth_provider or 'local'
            from datetime import datetime
            db_user.updated_at = datetime.utcnow()
            session.add(db_user)
            await session.commit()
    return RedirectResponse("/dashboard/profile?password_success=updated", status_code=302)
