from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from urllib.parse import urlencode
import secrets

router = APIRouter(tags=["auth"])

@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    import main
    if request.state.user:
        return RedirectResponse("/dashboard", status_code=302)
    return main._auth_template(request)

@router.api_route("/auth/guest", methods=["GET", "POST"])
async def auth_guest(request: Request):
    import main
    if not main._local_guest_enabled(request):
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    user = await main._get_or_create_local_guest()
    sid = await main.auth_services.SessionService.create(user.id)
    dest = request.query_params.get("next") or "/dashboard"
    if not dest.startswith("/") or dest.startswith("//"):
        dest = "/dashboard"
    response = RedirectResponse(dest, status_code=302)
    main._set_session_cookie(response, request, sid)
    return response

@router.get("/auth/reset", response_class=HTMLResponse)
async def auth_reset_page(request: Request):
    import main
    if request.state.user:
        return RedirectResponse("/dashboard", status_code=302)
    token = request.query_params.get("token", "")
    try:
        await main.auth_services.PasswordResetService.validate(token)
        return main._auth_template(request, active_tab="reset", reset_token=token)
    except main.auth_services.AuthError as exc:
        return main._auth_template(request, error=exc.message, active_tab="forgot", status_code=400)

@router.get("/auth/google")
async def auth_google(request: Request):
    import main
    if not main._google_oauth_configured():
        main.logger.warning("Google OAuth requested but configuration is incomplete")
        return main._auth_error_redirect("google_not_configured")

    settings = main._google_settings()
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings["client_id"],
        "redirect_uri": settings["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "include_granted_scopes": "true",
    }
    response = RedirectResponse(f"{main.GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        main.OAUTH_STATE_COOKIE, state, max_age=10 * 60, httponly=True, secure=main._cookie_secure(request), samesite="lax", path=main.OAUTH_STATE_COOKIE_PATH,
    )
    return response

@router.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    import main
    from core.database import AsyncSessionLocal
    from repositories.user_repo import user_repo
    import datetime
    
    google_error = request.query_params.get("error")
    if google_error:
        main.logger.info("Google OAuth callback returned error=%s", google_error)
        return main._auth_error_redirect("user_cancelled" if google_error == "access_denied" else "oauth_failed")

    expected_state = request.cookies.get(main.OAUTH_STATE_COOKIE)
    received_state = request.query_params.get("state", "")
    if not expected_state or not received_state or not secrets.compare_digest(expected_state, received_state):
        main.logger.warning("Google OAuth state validation failed")
        return main._auth_error_redirect("invalid_state")

    if not main._google_oauth_configured():
        main.logger.warning("Google OAuth callback received but configuration is incomplete")
        return main._auth_error_redirect("google_not_configured")

    code = request.query_params.get("code", "")
    if not code:
        main.logger.warning("Google OAuth callback missing authorization code")
        return main._auth_error_redirect("invalid_code")

    try:
        id_token_value = await main._exchange_google_code(code)
        profile = main._verify_google_profile(id_token_value)
    except main.OAuthInvalidCode:
        main.logger.exception("Google OAuth failed during code or ID token validation")
        return main._auth_error_redirect("invalid_code")
    except main.OAuthNoEmail:
        main.logger.warning("Google OAuth rejected because no email was returned")
        return main._auth_error_redirect("google_no_email")
    except main.OAuthEmailNotVerified:
        main.logger.warning("Google OAuth rejected because email was not verified")
        return main._auth_error_redirect("email_not_verified")
    except main.OAuthServiceError:
        main.logger.exception("Google OAuth service error")
        return main._auth_error_redirect("oauth_service_error")
    except Exception:
        main.logger.exception("Unexpected Google OAuth callback failure")
        return main._auth_error_redirect("oauth_failed")

    try:
        is_new_user = False
        async with AsyncSessionLocal() as session:
            user_by_google = await user_repo.get_by_google_id(session, profile["google_id"])
            user_by_email = await user_repo.get_by_email(session, profile["email"])

            if user_by_google and user_by_email and user_by_google.id != user_by_email.id:
                main.logger.warning("Google OAuth account conflict")
                return main._auth_error_redirect("account_conflict")

            if user_by_google:
                user = user_by_google
                user.email = profile["email"]
                user.avatar_url = profile["avatar_url"]
                user.email_verified = 1 if profile["email_verified"] else 0
                session.add(user)
                await session.commit()
            elif user_by_email:
                user = user_by_email
                user.google_id = profile["google_id"]
                user.avatar_url = profile["avatar_url"]
                user.email_verified = 1 if profile["email_verified"] else 0
                if user.auth_provider == 'local':
                    user.auth_provider = 'hybrid'
                session.add(user)
                await session.commit()
            else:
                user = await user_repo.create(session, obj_in={
                    "email": profile["email"],
                    "google_id": profile["google_id"],
                    "name": profile["name"],
                    "avatar_url": profile["avatar_url"],
                    "email_verified": 1 if profile["email_verified"] else 0,
                    "auth_provider": "google",
                })
                is_new_user = True

        sid = await main.auth_services.SessionService.create(user.id)
        response = RedirectResponse(main._oauth_destination(user, is_new_user), status_code=302)
        main._set_session_cookie(response, request, sid)
        response.delete_cookie(main.OAUTH_STATE_COOKIE, path=main.OAUTH_STATE_COOKIE_PATH)
        return response
    except Exception:
        main.logger.exception("Unexpected Google OAuth account persistence failure")
        return main._auth_error_redirect("oauth_failed")


@router.post("/auth/register")
async def auth_register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
    name: str = Form(""),
    csrf_token: str = Form(""),
):
    import main
    values = {
        "email": main.auth_services.safe_form_value(email, 254),
        "name": main.auth_services.safe_form_value(name, 80),
    }
    try:
        await main._verify_auth_csrf(request, csrf_token)
        user = await main.auth_services.AuthService.register(
            email=email, password=password, confirm_password=confirm_password, name=name, key=main.auth_services.client_key(request, "register"),
        )
    except main.auth_services.AuthError as exc:
        return main._auth_template(request, error=exc.message, active_tab="register", field=exc.field, values=values, status_code=exc.status_code)

    sid = await main.auth_services.SessionService.create(user.id)
    verification = await main._prepare_and_send_verification(request, user, rate_limit=False)
    verify_param = "sent" if verification.get("ok") else "unavailable"
    response = RedirectResponse(f"/dashboard?verify={verify_param}", status_code=302)
    main._set_session_cookie(response, request, sid)
    return response

@router.post("/auth/login")
async def auth_login(
    request: Request,
    email: str = Form(""),
    phone: str = Form(""),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    import main
    identity = email or phone
    values = {"email": main.auth_services.safe_form_value(identity, 254)}
    try:
        await main._verify_auth_csrf(request, csrf_token)
        user = await main.auth_services.AuthService.login(
            email=identity, password=password, key=main.auth_services.client_key(request, "login"),
        )
    except main.auth_services.AuthError as exc:
        return main._auth_template(request, error=exc.message, active_tab="login", field=exc.field, values=values, status_code=exc.status_code)

    sid = await main.auth_services.SessionService.create(user.id)
    dest = "/dashboard"
    response = RedirectResponse(dest, status_code=302)
    main._set_session_cookie(response, request, sid)
    return response

@router.post("/auth/forgot-password")
async def auth_forgot_password(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(""),
):
    import main
    values = {"email": main.auth_services.safe_form_value(email, 254)}
    try:
        await main._verify_auth_csrf(request, csrf_token)
        reset = await main.auth_services.PasswordResetService.request(
            email=email, key=main.auth_services.client_key(request, "forgot"),
        )
    except main.auth_services.AuthError as exc:
        return main._auth_template(request, error=exc.message, active_tab="forgot", field=exc.field, values=values, status_code=exc.status_code)

    dev_reset_url = None
    if reset.get("sent"):
        try:
            await main._send_password_reset_email(request, reset)
        except main.EmailServiceUnavailable:
            main.logger.warning("Password reset email is not configured or failed")
            if main.settings.app_env.lower() not in {"prod", "production"}:
                dev_reset_url = main._password_reset_url(request, reset["token"])

    return main._auth_template(request, active_tab="forgot", success_message="Если аккаунт существует, мы отправили ссылку для восстановления.", values=values, dev_reset_url=dev_reset_url)

@router.post("/auth/reset-password")
async def auth_reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    import main
    try:
        await main._verify_auth_csrf(request, csrf_token)
        user = await main.auth_services.PasswordResetService.reset(
            token=token, password=password, confirm_password=confirm_password, key=main.auth_services.client_key(request, "reset"),
        )
    except main.auth_services.AuthError as exc:
        return main._auth_template(request, error=exc.message, active_tab="reset" if exc.code not in {"invalid_reset_token", "expired_reset_token", "used_reset_token"} else "forgot", field=exc.field, reset_token=token, status_code=exc.status_code)

    sid = await main.auth_services.SessionService.create(user.id)
    response = RedirectResponse("/dashboard?success=password_reset", status_code=302)
    main._set_session_cookie(response, request, sid)
    return response

@router.post("/auth/logout")
async def auth_logout(request: Request):
    import main
    sid = request.cookies.get("sid")
    if sid:
        await main.auth_services.SessionService.delete(sid)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("sid")
    return response

@router.post("/auth/send-email-verification")
async def auth_send_email_verification(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._verification_json("verification_failed", status_code=401)
    result = await main._prepare_and_send_verification(request, user)
    if not result.get("ok"):
        code = result.get("error", "verification_failed")
        return main._verification_json(code, status_code=429 if code in {"resend_cooldown", "resend_rate_limited"} else 400, retry_after=int(result.get("retry_after") or 0))
    return JSONResponse({"ok": True, "message": main.AUTH_SUCCESS_MESSAGES["verification_sent"], "retry_after": main.EMAIL_RESEND_COOLDOWN_SECONDS})

@router.post("/auth/resend-email-verification")
async def auth_resend_email_verification(request: Request):
    return await auth_send_email_verification(request)

@router.get("/auth/verify-email")
async def auth_verify_email(request: Request):
    import main
    from core.database import AsyncSessionLocal
    from repositories.user_repo import user_repo
    import hmac, time
    import hashlib
    if main._verify_attempt_limited(request):
        return RedirectResponse("/auth?error=invalid_token", status_code=302)

    token = request.query_params.get("token", "")
    if not token:
        return RedirectResponse("/auth?error=invalid_token", status_code=302)
        
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        import models.user
        result = await session.execute(select(models.user.User).where(models.user.User.email_verify_token == token_hash))
        user = result.scalars().first()
        
        if not user:
            return RedirectResponse("/auth?error=invalid_token", status_code=302)
            
        stored = user.email_verify_token or ""
        if not hmac.compare_digest(stored, token_hash):
            return RedirectResponse("/auth?error=invalid_token", status_code=302)
            
        expires_at = int(user.email_verify_expires or 0)
        if expires_at < int(time.time()):
            user.email_verify_token = None
            user.email_verify_expires = None
            user.verification_sent_at = None
            session.add(user)
            await session.commit()
            return RedirectResponse("/auth?error=expired_token", status_code=302)
            
        user.email_verified = 1
        user.email_verify_token = None
        user.email_verify_expires = None
        user.verification_sent_at = None
        session.add(user)
        await session.commit()

    sid = await main.auth_services.SessionService.create(user.id)
    dest = "/dashboard?email_success=email_verified"
    response = RedirectResponse(dest, status_code=302)
    main._set_session_cookie(response, request, sid)
    return response