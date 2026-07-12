from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

router = APIRouter(tags=["payments"])

@router.post("/api/billing/promo-credits/purchase")
async def api_billing_promo_purchase(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    payload = await main._json_body(request)
    amount = payload.get("amount", 0)
    return JSONResponse(main.services.PromoService.purchase_credits(user.id, amount))

@router.get("/api/billing/credit-logs")
async def api_billing_credit_logs(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    return JSONResponse({"ok": True, "logs": await main.repositories.log_repo.promo_credit_log_repo.get_multi_by_user(session, user.id)})

@router.get("/payment", response_class=HTMLResponse)
async def payment_page(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return RedirectResponse("/auth?next=/payment", status_code=302)
    reason = request.query_params.get("reason", "")
    pkg_type = request.query_params.get("type", "slot")
    context = {"user": user, "reason": reason, "pkg_type": pkg_type}
    if pkg_type == "slot":
        context["package"] = next((p for p in main.PAYMENT_PACKAGES if p["type"] == "slot"), None)
    elif pkg_type == "credits":
        amount_str = request.query_params.get("amount", "100")
        try:
            amt = int(amount_str)
        except ValueError:
            amt = 100
        context["package"] = next((p for p in main.PAYMENT_PACKAGES if p["type"] == "credits" and p["credits"] == amt), None)
    else:
        context["package"] = None
    if not context["package"]:
        return RedirectResponse("/dashboard", status_code=302)
    return main.templates.TemplateResponse(request, "payment.html", context)

@router.post("/payment/create")
async def payment_create(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    body = await request.json()
    pkg_id = body.get("package_id")
    pkg = next((p for p in main.PAYMENT_PACKAGES if p["id"] == pkg_id), None)
    if not pkg:
        return JSONResponse({"error": "Неверный пакет"}, status_code=400)
    domain = main.settings.domain or "lendings.kz"
    proto = "https" if main.settings.app_env in ("prod", "production") else "http"
    site_slug = body.get("site_slug")
    order_id = main.services.PaymentService.create_order(user.id, pkg, site_slug=site_slug)
    try:
        url = main.services.PaymentService.generate_kaspi_url(
            order_id, pkg["price"], pkg["name"], success_url=f"{proto}://{domain}/payment/status/{order_id}"
        )
        return JSONResponse({"ok": True, "url": url})
    except Exception as e:
        main.logger.exception("Failed to generate payment url")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/payment/status/{order_id}")
async def payment_status(order_id: str, request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    status = main.services.PaymentService.check_order(order_id, user.id)
    if not status:
        return main.templates.TemplateResponse(request, "payment_status.html", {"status": "not_found", "user": user})
    return main.templates.TemplateResponse(request, "payment_status.html", {
        "status": status["status"],
        "package_name": status["package_name"],
        "user": user,
    })

@router.post("/payment/webhook")
async def payment_webhook(request: Request):
    import main
    sig = request.headers.get("X-Signature", "")
    body = await request.body()
    # Kaspi verification mock/logic
    # In production, verify signature
    payload = main.json.loads(body)
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    main.services.PaymentService.process_webhook(order_id, amount)
    return JSONResponse({"ok": True})
