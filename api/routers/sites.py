from fastapi import APIRouter, Request, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import re
import json
import base64
import uuid

router = APIRouter(tags=["sites"])

def _require_auth(request):
    import main
    return main._require_auth(request)

import main

@router.get("/site/{slug}", response_class=HTMLResponse)
async def serve_site(slug: str):
    # sanitise slug to prevent path traversal
    slug = re.sub(r'[^a-zA-Z0-9_-]', '', slug)
    site = main.db.get_site_by_slug(slug)
    if site:
        site = main.services.SupportService.refresh_site(site["id"]) or site
        if not main.services.is_support_public(site.get("support_status")):
            return HTMLResponse(main.services.maintenance_page(), status_code=503)
    path = main.GENERATED_DIR / f"{slug}.html"
    if not path.exists():
        return HTMLResponse("<h1>Сайт не найден</h1>", status_code=404)
    html = path.read_text(encoding="utf-8")
    if "window.self !== window.top" not in html and "window.__lendingsAnalytics" in html:
        html = html.replace("if (window.__lendingsAnalytics) return;", "if (window.__lendingsAnalytics) return;\n  try { if (window.self !== window.top) return; } catch(e) { return; }")
    return HTMLResponse(html)





# ── Upload photo ──────────────────────────────────────────────────────────────

@router.post("/upload-photo")
async def upload_photo(file: UploadFile = File(...)):
    # save photo to disk and return url
    content = await file.read()

    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(content))
        img.thumbnail((900, 900))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        content = buf.getvalue()
        ext = "jpg"
    except ImportError:
        ext = (file.filename or "photo").rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "webp"):
            ext = "jpg"

    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    (main.UPLOADS_DIR / filename).write_bytes(content)
    url = f"/static/uploads/{filename}"
    
    # Run OCR in background or await it
    base64_img = base64.b64encode(content).decode("utf-8")
    ocr_text = await main._run_ocr(base64_img)
    
    return JSONResponse({"url": url, "size": len(content), "ocr_text": ocr_text})


# ── Chat / site generation ────────────────────────────────────────────────────




# ── Site edit ─────────────────────────────────────────────────────────────────

@router.post("/site/{slug}/edit")
async def site_edit(slug: str, request: Request, bg_tasks: BackgroundTasks):
    # handle ai site edit and generation flow
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)

    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = main.db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    site = main.services.SupportService.refresh_site(site["id"]) or site
    if not main.services.is_support_operational(site.get("support_status")):
        return JSONResponse({"error": "Поддержка сайта не активна. Оплатите поддержку, чтобы редактировать сайт."}, status_code=402)

    body           = await request.json()
    message        = body.get("message", "").strip()
    edit_history   = body.get("edit_history", [])
    client_history = body.get("history", [])
    new_photo_urls = body.get("photo_urls", [])

    if not message:
        return JSONResponse({"error": "Пустой запрос"}, status_code=400)

    # Load site data to build context for edit chat
    data = site.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data)

    # Build plain-text context from site data so AI knows what's on the site
    site_context = "\n".join(filter(None, [
        f"Название/профессия: {data.get('name', '')}",
        f"Услуги: {data.get('services', '')}",
        f"Контакт: {data.get('city', '')}",
        f"Стиль: {data.get('vibe', '')}",
        f"Фото на сайте: {'есть (' + str(len(data.get('photo_urls', []))) + ' шт.)' if data.get('photo_urls') else 'нет'}",
    ]))

    ai_message = message
    if new_photo_urls:
        ai_message += f"\n[Системное уведомление: клиент прикрепил {len(new_photo_urls)} фото к сообщению]"
    
    # We still want to save the original message in history without the system tag,
    # but for the AI prompt we'll pass the ai_message
    ai_history = edit_history + [{"role": "user", "content": ai_message}]
    edit_history = edit_history + [{"role": "user", "content": message}]

    result       = _ai_edit_chat(ai_history, site_context)
    reply        = result.get("reply", "Понял!")
    ready        = result.get("ready", False)
    needs_photos = result.get("needs_photos", False)
    edit_summary = result.get("edit_summary") or message

    edit_history = edit_history + [{"role": "assistant", "content": reply}]

    # Needs photos — tell client to upload, don't generate yet
    if needs_photos and not new_photo_urls:
        return JSONResponse({
            "done":         False,
            "needs_photos": True,
            "message":      reply,
            "edit_history": edit_history,
        })

    # Not ready yet — clarifying question
    if not ready:
        return JSONResponse({
            "done":         False,
            "message":      reply,
            "edit_history": edit_history,
        })

    # Ready — generate
    if _dev_credits(user) < 1:
        return JSONResponse({"error": "Недостаточно кредитов разработки"}, status_code=402)

    business_check = main.services.PromotionService.validate_business_change(site, edit_summary)
    if not business_check.get("ok"):
        return JSONResponse({"error": business_check["message"]}, status_code=400)

    data = site.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data)

    stored_history   = data.get("chat_history", [])
    combined_history = client_history if client_history else stored_history

    # Merge new photos into existing
    if new_photo_urls:
        existing_photos = data.get("photo_urls", [])
        data["photo_urls"] = existing_photos + new_photo_urls

    prev_html = (main.GENERATED_DIR / f"{slug}.html").read_text(encoding="utf-8") if (main.GENERATED_DIR / f"{slug}.html").exists() else ""
    data["edit_request"]   = edit_summary
    data["chat_history"]   = combined_history
    data["prev_html_full"] = prev_html

    main.db.update_site_edit_status(site["id"], "editing")
    
    bg_tasks.add_task(_background_edit_task, user, site, slug, data, prev_html, combined_history, edit_history)

    return JSONResponse({
        "done":         False,
        "status":       "editing",
        "message":      reply + " Запускаю Агента для внесения правок...",
        "edit_history": edit_history,
    })

async def _background_edit_task(user: dict, site: dict, slug: str, data: dict, prev_html: str, combined_history: list, edit_history: list):
    try:
        gen = _ai_generate(data)

        gen_in  = gen["input_tokens"]
        gen_out = gen["output_tokens"]
        gen_cr  = gen["cache_read_tokens"]
        gen_cc  = gen["cache_create_tokens"]
        cost       = _calc_cost(gen_in, gen_out, gen_cr, gen_cc)
        our_tokens = _tokens_to_ours(gen_in, gen_out)

        fresh_user = main.db.get_user_by_id(user["id"]) or user
        if _dev_credits(fresh_user) < our_tokens:
            main.db.update_site_edit_status(site["id"], "error_credits")
            return

        if prev_html:
            main.services.VersionService.create_snapshot(site["id"], prev_html, site.get("data") or {}, "before_site_edit")
            
        deducted = main.db.deduct_tokens(
            user_id=user["id"], amount=our_tokens,
            reason=f"site_edit:{slug}",
            site_id=site["id"],
            claude_in=gen_in, claude_out=gen_out,
            cache_read=gen_cr, cost_usd=cost,
        )
        if not deducted:
            main.db.update_site_edit_status(site["id"], "error_balance")
            return

        updated_html = _inject_analytics(gen["html"], slug)
        (main.GENERATED_DIR / f"{slug}.html").write_text(updated_html, encoding="utf-8")

        data_to_save = {**data, "chat_history": combined_history}
        main.db.update_site_data(site["id"], data_to_save)
        main.db.update_site_html(site["id"], str(main.GENERATED_DIR / f"{slug}.html"), our_tokens)
        main.services.VersionService.create_snapshot(site["id"], updated_html, data_to_save, "site_edit")
        main.services.CampaignService.site_changed(site["id"], "site_edit")
        
        main.db.update_site_edit_status(site["id"], "ready")
    except Exception as e:
        main.logger.error(f"Error in background edit task for {slug}: {e}")
        main.db.update_site_edit_status(site["id"], "error")

@router.get("/site/{slug}/edit/status")
async def get_site_edit_status(slug: str, request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
        
    site = main.db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return JSONResponse({"error": "Not found"}, status_code=404)
        
    return JSONResponse({"status": site.get("edit_status", "ready")})


# ── Payment routes ────────────────────────────────────────────────────────────




@router.post("/site/{slug}/delete")
async def site_delete(slug: str, request: Request):
    # site delete
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = main.db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return JSONResponse({"error": "Не найдено"}, status_code=404)
    main.db.delete_site(site["id"], user["id"])
    # Remove generated HTML file
    html_file = main.GENERATED_DIR / f"{slug}.html"
    if html_file.exists():
        html_file.unlink()
    return RedirectResponse("/dashboard", status_code=302)


def _owned_site(slug: str, user: dict) -> dict | None:
    # owned site
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = main.db.get_site_by_slug(slug)
    if not site or site["user_id"] != user["id"]:
        return None
    return site


@router.get("/api/sites/{slug}/support")
async def api_support_status(slug: str, request: Request):
    # api support status
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    site = main.services.SupportService.refresh_site(site["id"]) or site
    return JSONResponse({
        "ok": True,
        "status": site.get("support_status"),
        "support_paid_until": site.get("support_paid_until"),
        "invoice": main.services.SupportService.get_open_invoice(site["id"]),
    })


@router.post("/api/sites/{slug}/analytics/events")
async def api_site_analytics_event(slug: str, request: Request):
    # public endpoint used by generated sites
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    site = main.db.get_site_by_slug(slug)
    if not site:
        return JSONResponse({"ok": False}, status_code=404)
    site = main.services.SupportService.refresh_site(site["id"]) or site
    try:
        body = await request.json()
    except Exception:
        body = {}
    main.services.AnalyticsService.record_event(
        site["id"],
        body.get("event_type") or "cta_click",
        body.get("payload") or {},
    )
    return JSONResponse({"ok": True})


@router.post("/api/sites/{slug}/support/pay")
async def api_support_pay(slug: str, request: Request):
    # api support pay
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    site = main.services.SupportService.refresh_site(site["id"]) or site
    invoice = main.services.SupportService.get_open_invoice(site["id"])
    if not invoice:
        return JSONResponse({"ok": False, "error": "support_active", "message": "Поддержка уже активна."}, status_code=400)
    body = await request.json()
    phone_clean = re.sub(r"[^\d]", "", body.get("phone") or user.get("phone") or "")
    if len(phone_clean) < 10:
        return JSONResponse({"ok": False, "error": "phone_required", "message": "Введите номер телефона Kaspi."}, status_code=400)
    order_id = _payment_order_id()
    try:
        data = _kaspi_invoice(
            phone_clean,
            order_id,
            f"lendings.kz поддержка сайта {site['slug']}",
            amount=int(invoice["amount"]),
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Ошибка платежного шлюза: {exc}"}, status_code=502)
    if not data.get("id"):
        return JSONResponse({"ok": False, "error": "Kaspi не принял платёж", "detail": data}, status_code=400)
    main.db.create_payment(
        user_id=user["id"],
        order_id=order_id,
        invoice_id=str(data["id"]),
        amount=int(invoice["amount"]),
        tokens=0,
        status="pending",
        payment_kind="support_invoice",
        dev_credits=0,
        promo_credits=0,
        site_id=site["id"],
        support_invoice_id=invoice["id"],
    )
    return JSONResponse({
        "ok": True,
        "invoice_id": data["id"],
        "order_id": order_id,
        "amount": int(invoice["amount"]),
        "message": f"Запрос отправлен на номер +{phone_clean}. Откройте Kaspi и подтвердите оплату.",
    })


@router.post("/api/sites/{slug}/promotion/setup")
async def api_promotion_setup(slug: str, request: Request):
    # api promotion setup
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    result = main.services.PromotionService.setup(user["id"], site["id"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/sites/{slug}/promotion/forecast")
async def api_promotion_forecast(slug: str, request: Request):
    # api promotion forecast
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    body = await request.json()
    try:
        credits = int(body.get("credits") or 0)
        duration_hours = int(body.get("duration_hours") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_campaign", "message": "Введите корректный бюджет и длительность."}, status_code=400)
    result = main.services.CampaignService.forecast(
        user["id"],
        site["id"],
        credits,
        duration_hours,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/sites/{slug}/promotion/campaigns")
async def api_campaign_launch(slug: str, request: Request):
    # api campaign launch
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    body = await request.json()
    try:
        credits = int(body.get("credits") or 0)
        duration_hours = int(body.get("duration_hours") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_campaign", "message": "Введите корректный бюджет и длительность."}, status_code=400)
    result = main.services.CampaignService.launch(
        user["id"],
        site["id"],
        credits,
        duration_hours,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/sites/{slug}/promotion/campaigns")
async def api_campaign_history(slug: str, request: Request):
    # api campaign history
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    return JSONResponse({"ok": True, "campaigns": main.services.CampaignService.history(site["id"])})


@router.get("/api/sites/{slug}/promotion/campaigns/{campaign_id}")
async def api_campaign_status(slug: str, campaign_id: int, request: Request):
    # api campaign status
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    campaigns = main.services.CampaignService.history(site["id"])
    campaign = next((c for c in campaigns if int(c["id"]) == int(campaign_id)), None)
    if not campaign:
        return JSONResponse({"error": "Кампания не найдена"}, status_code=404)
    return JSONResponse({"ok": True, "campaign": campaign})
@router.post("/api/sites/{slug}/analytics/purchase")
async def api_purchase_analytics(slug: str, request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
        
    if site["data"].get("analytics_purchased"):
        return JSONResponse({"error": "Уже подключено"}, status_code=400)
        
    price = 200
    fresh_user = main.db.get_user_by_id(user["id"])
    if (fresh_user.get("dev_credits") or 0) < price:
        return JSONResponse({"error": "Недостаточно кредитов разработки"}, status_code=402)
        
    deducted = main.db.deduct_tokens(
        user_id=user["id"], amount=price,
        reason=f"analytics_purchase:{slug}",
        site_id=site["id"],
        claude_in=0, claude_out=0, cache_read=0, cost_usd=0.0
    )
    if not deducted:
        return JSONResponse({"error": "Не удалось списать кредиты"}, status_code=409)
        
    data = site["data"]
    data["analytics_purchased"] = True
    main.db.update_site_data(site["id"], data)
    
    return JSONResponse({"ok": True})


@router.post("/api/sites/{slug}/analytics/restore")
async def api_restore_analytics(slug: str, request: Request):
    # api restore analytics
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    result = main.services.AnalyticsService.restore(user["id"], site["id"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/sites/{slug}/versions")
async def api_site_versions(slug: str, request: Request):
    # api site versions
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    return JSONResponse({"ok": True, "versions": main.services.VersionService.list_versions(user["id"], site["id"])})


@router.post("/api/sites/{slug}/versions/{version_id}/restore")
async def api_restore_version(slug: str, version_id: int, request: Request):
    # api restore version
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    site = _owned_site(slug, user)
    if not site:
        return JSONResponse({"error": "Сайт не найден"}, status_code=404)
    result = main.services.VersionService.restore(user["id"], site["id"], version_id)
    if result.get("ok"):
        restored_html = _inject_analytics(result["html"], site["slug"])
        (main.GENERATED_DIR / f"{site['slug']}.html").write_text(restored_html, encoding="utf-8")
        main.db.update_site_html(site["id"], str(main.GENERATED_DIR / f"{site['slug']}.html"), site.get("tokens_used") or 0)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)
