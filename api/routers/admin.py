from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from core.templating import templates
from core.config import settings
import auth_services
import db

router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_SESSION_COOKIE = "admin_sid2"
ADMIN_CSRF_COOKIE = "admin_csrf2"

def _cookie_secure(request: Request) -> bool:
    if request.url.hostname in {"localhost", "127.0.0.1"}:
        return False
    proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or proto == "https" or settings.app_env.lower() in {"prod", "production"}

def _set_admin_session_cookie(response: RedirectResponse, request: Request, sid: str):
    response.set_cookie(ADMIN_SESSION_COOKIE, sid, httponly=True, secure=_cookie_secure(request), samesite="lax", path="/admin", max_age=365 * 24 * 3600)

def _set_admin_csrf_cookie(response, request: Request, token: str):
    response.set_cookie(ADMIN_CSRF_COOKIE, token, httponly=True, secure=_cookie_secure(request), samesite="lax", path="/admin", max_age=2 * 3600)

def _require_admin(request: Request):
    return db.get_admin_by_session(request.cookies.get(ADMIN_SESSION_COOKIE))

def _admin_registration_allowed(setup_key: str = "") -> bool:
    import hmac
    required = settings.admin_registration_key.strip()
    if required:
        return bool(setup_key and hmac.compare_digest(required, setup_key))
    if db.admin_count() == 0:
        return True
    return False

def _admin_auth_template(request: Request, *, error: str = "", active_tab: str = "login", status_code: int = 200, values: dict | None = None, setup_key: str | None = None):
    setup_key = request.query_params.get("setup_key", "") if setup_key is None else setup_key
    csrf_token = auth_services.CsrfService.generate()
    response = templates.TemplateResponse(
        request, "admin_auth.html", 
        {"csrf_token": csrf_token, "error": error, "active_tab": active_tab, "values": values or {}, "has_admins": db.admin_count() > 0, "registration_open": _admin_registration_allowed(setup_key), "setup_key": setup_key}, 
        status_code=status_code
    )
    _set_admin_csrf_cookie(response, request, csrf_token)
    return response

def _verify_admin_csrf(request: Request, csrf_token: str):
    auth_services.CsrfService.verify(csrf_token, request.cookies.get(ADMIN_CSRF_COOKIE))

@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse("/admin/login", status_code=302)
    csrf_token = auth_services.CsrfService.generate()
    response = templates.TemplateResponse(request, "admin.html", {"admin": admin, "csrf_token": csrf_token})
    _set_admin_csrf_cookie(response, request, csrf_token)
    return response

@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if _require_admin(request):
        return RedirectResponse("/admin", status_code=302)
    return _admin_auth_template(request, active_tab="login")

@router.get("/register", response_class=HTMLResponse)
async def admin_register_page(request: Request):
    if _require_admin(request):
        return RedirectResponse("/admin", status_code=302)
    if not _admin_registration_allowed(request.query_params.get("setup_key", "")):
        return _admin_auth_template(request, active_tab="login", error="Регистрация админов закрыта. Войдите или используйте server setup key.", status_code=403)
    return _admin_auth_template(request, active_tab="register")

@router.post("/login")
async def admin_login(request: Request, email: str = Form(""), password: str = Form(""), csrf_token: str = Form("")):
    try:
        _verify_admin_csrf(request, csrf_token)
        email = auth_services.validate_email(email)
    except auth_services.AuthError as exc:
        return _admin_auth_template(request, active_tab="login", error=exc.message, status_code=exc.status_code)

    admin = db.verify_admin_password(email, password)
    if not admin:
        return _admin_auth_template(request, active_tab="login", error="Неверный email или пароль.", status_code=401, values={"email": email})
    response = RedirectResponse("/admin", status_code=302)
    _set_admin_session_cookie(response, request, db.create_admin_session(admin["id"]))
    return response

@router.post("/register")
async def admin_register(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""), confirm_password: str = Form(""), setup_key: str = Form(""), csrf_token: str = Form("")):
    values = {"name": name, "email": email}
    try:
        _verify_admin_csrf(request, csrf_token)
        if not _admin_registration_allowed(setup_key):
            return _admin_auth_template(request, active_tab="login", error="Регистрация админов закрыта.", status_code=403)
        email = auth_services.validate_email(email)
        name = auth_services.validate_name(name)
        auth_services.validate_password(password, confirm_password, email=email, name=name)
    except auth_services.AuthError as exc:
        return _admin_auth_template(request, active_tab="register", error=exc.message, status_code=exc.status_code, values=values, setup_key=setup_key)

    admin = db.create_admin_user(email, password, name)
    if not admin:
        return _admin_auth_template(request, active_tab="register", error="Не удалось создать админа. Возможно, email уже используется.", status_code=400, values=values, setup_key=setup_key)
    response = RedirectResponse("/admin", status_code=302)
    _set_admin_session_cookie(response, request, db.create_admin_session(admin["id"]))
    return response

@router.post("/logout")
async def admin_logout(request: Request, csrf_token: str = Form("")):
    try:
        _verify_admin_csrf(request, csrf_token)
    except auth_services.AuthError:
        pass
    db.delete_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response

@router.get("/api/stats")
async def admin_api_stats(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(db.admin_stats())

@router.get("/api/users")
async def admin_api_users(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(db.admin_users())

@router.get("/api/user/{uid}")
async def admin_api_user(uid: int, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    detail = db.admin_user_detail(uid)
    if not detail:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)

@router.post("/api/user/{uid}/add-tokens")
async def admin_add_tokens(uid: int, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    db.add_tokens(uid, amount, "admin_grant")
    updated = db.get_user_by_id(uid)
    return JSONResponse({"ok": True, "tokens": updated["tokens"], "dev_credits": updated["dev_credits"]})

@router.post("/api/user/{uid}/add-slots")
async def admin_add_slots(uid: int, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    db.add_site_slots_only(uid, amount, "admin_grant_slots")
    updated = db.get_user_by_id(uid)
    return JSONResponse({"ok": True, "site_slots": updated["site_slots"]})
