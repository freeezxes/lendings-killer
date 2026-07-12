from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from services.ai_service import _ai_chat
import json

router = APIRouter(tags=["chat"])

@router.get("/start")
async def start(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    onboarding = main.services.OnboardingService.current(user["id"])
    return JSONResponse({
        "message": "Привет! Расскажи о своём бизнесе — кто ты и чем занимаешься?",
        "history": onboarding["session"].get("history") if onboarding.get("session") else [],
        "session": onboarding.get("session"),
        "summary": onboarding.get("summary", []),
        "progress": onboarding.get("progress", 0),
        "done": False,
    })

@router.post("/chat")
async def chat(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")

    body = await main._json_body(request)
    message = str(body.get("message") or "").strip()
    raw_session_id = body.get("session_id")
    with open("/tmp/chat_debug.log", "a") as f:
        f.write(f"RECEIVED HISTORY LENGTH: {len(body.get('history', []))}\n")
        f.write(f"RECEIVED MESSAGE: {message}\n")
    session_id = int(raw_session_id) if str(raw_session_id or "").isdigit() else None
    if len(message) > 4000:
        return main._api_error("Invalid draft payload", 400, "invalid_payload")
    history = main.services.OnboardingService._safe_history(body.get("history"))
    photo_urls = main.services.OnboardingService._safe_photo_urls(body.get("photo_urls"))
    try:
        acc_chat_in = max(0, int(body.get("chat_in") or 0))
        acc_chat_out = max(0, int(body.get("chat_out") or 0))
        acc_chat_cr = max(0, int(body.get("chat_cr") or 0))
    except (TypeError, ValueError):
        return main._api_error("Invalid draft payload", 400, "invalid_payload")

    if not message:
        return main._api_error("Empty message", 400, "empty_message")

    try:
        session = main.db.upsert_onboarding_session(user["id"], session_id)
        session_id = session["id"]
    except main.db.DraftLimitError:
        return main._api_error("Draft limit reached", 409, "draft_limit_reached")
    except main.db.DraftConflictError:
        return main._api_error("Draft not found", 404, "draft_not_found")

    history.append({"role": "user", "content": message})

    result     = main._ai_chat(history)
    reply      = result.get("reply", "Продолжай, я слушаю")
    ready      = result.get("ready", False)
    collected  = result.get("collected", {})
    usage      = result.get("_usage", {})

    acc_chat_in  += usage.get("inp", 0)
    acc_chat_out += usage.get("out", 0)
    acc_chat_cr  += usage.get("cr",  0)

    history.append({"role": "assistant", "content": reply})

    if not ready:
        session = main.db.upsert_onboarding_session(
            user["id"], session_id, status="draft", history=history, collected=collected, photo_urls=photo_urls, chat_in=acc_chat_in, chat_out=acc_chat_out, chat_cr=acc_chat_cr,
        )
        presented = main.services.OnboardingService.present(session)
        with open("/tmp/chat_debug.log", "a") as f:
            f.write(f"SENDING HISTORY LENGTH: {len(history)}\n")
        return JSONResponse({
            "message":  reply, "history":  history, "done": False, "session_id": session["id"], "collected": collected, "summary": presented["summary"], "progress": presented["progress"], "chat_in":  acc_chat_in, "chat_out": acc_chat_out, "chat_cr":  acc_chat_cr,
        })

    session = main.db.upsert_onboarding_session(
        user["id"], session_id, status="ready", history=history, collected=collected, photo_urls=photo_urls, chat_in=acc_chat_in, chat_out=acc_chat_out, chat_cr=acc_chat_cr,
    )
    presented = main.services.OnboardingService.present(session)
    return JSONResponse({
        "message": reply, "history": history, "done": False, "ready": True, "confirm_required": True, "session_id": session["id"], "collected": collected, "summary": presented["summary"], "progress": presented["progress"], "chat_in": acc_chat_in, "chat_out": acc_chat_out, "chat_cr": acc_chat_cr,
    })

@router.get("/api/onboarding/session")
async def api_onboarding_session(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    try:
        return JSONResponse({"ok": True, **main.services.OnboardingService.current(user["id"])})
    except main.db.DraftLimitError:
        return main._api_error("Draft limit reached", 409, "draft_limit_reached")

@router.post("/api/onboarding/session")
async def api_onboarding_autosave(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    payload = await main._json_body(request)
    with open("/tmp/chat_debug.log", "a") as f:
        f.write(f"AUTOSAVE RECEIVED HISTORY LENGTH: {len(payload.get('history', []))}\n")
        f.write(f"AUTOSAVE PAYLOAD: {payload}\n")
    try:
        return JSONResponse({"ok": True, **main.services.OnboardingService.autosave(user["id"], payload)})
    except main.db.DraftLimitError:
        return main._api_error("Draft limit reached", 409, "draft_limit_reached")
    except main.db.DraftConflictError:
        return main._api_error("Draft not found", 404, "draft_not_found")

@router.post("/api/onboarding/reset")
async def api_onboarding_reset(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    try:
        return JSONResponse({"ok": True, **main.services.OnboardingService.reset(user["id"])})
    except main.db.DraftLimitError:
        return main._api_error("Draft limit reached", 409, "draft_limit_reached")

@router.delete("/api/onboarding/session/{session_id}")
async def api_onboarding_delete(session_id: int, request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    result = main.services.OnboardingService.delete(user["id"], session_id)
    if not result.get("deleted"):
        return main._api_error("Draft not found", 404, "draft_not_found")
    return JSONResponse({"ok": True, **result})

@router.patch("/api/onboarding/session/{session_id}/title")
async def api_onboarding_rename(session_id: int, request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    payload = await main._json_body(request)
    try:
        return JSONResponse({"ok": True, **main.services.OnboardingService.rename(user["id"], session_id, payload.get("title"))})
    except main.DraftValidationError:
        return main._api_error("Invalid draft name", 400, "invalid_draft_name")
    except main.db.DraftConflictError:
        return main._api_error("Draft not found", 404, "draft_not_found")

@router.patch("/api/onboarding/sessions/order")
async def api_onboarding_reorder(request: Request):
    import main
    user = main._require_auth(request)
    if not user:
        return main._api_error("Authentication required", 401, "auth_required")
    payload = await main._json_body(request)
    session_ids = payload.get("session_ids") if isinstance(payload.get("session_ids"), list) else []
    return JSONResponse({"ok": True, **main.services.OnboardingService.reorder(user["id"], session_ids)})

@router.post("/api/onboarding/generate")
async def api_onboarding_generate(request: Request, background_tasks: BackgroundTasks):
    import main
    user = main._require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    body = await request.json()
    session_id = int(body.get("session_id") or 0)
    session = main.db.get_onboarding_session(session_id, user["id"])
    if not session:
        return JSONResponse({"error": "Черновик не найден"}, status_code=404)
    if session.get("status") not in {"draft", "ready", "failed", "completed", "generating"}:
        return JSONResponse({"error": "Сначала завершите ответы и подтвердите запуск."}, status_code=400)
    
    if session.get("status") == "generating":
        return JSONResponse({"ok": True, "status": "generating"})
        
    with main.db.get_conn() as c:
        c.execute("UPDATE onboarding_sessions SET status='generating' WHERE id=? AND user_id=?", (session_id, user["id"]))
    background_tasks.add_task(main._background_generate_task, user["id"], session)
    return JSONResponse({"ok": True, "status": "generating"})

@router.get("/api/onboarding/status")
async def api_onboarding_status(request: Request, session_id: int):
    import main
    user = main._require_auth(request)
    if not user:
        return JSONResponse({"error": "Требуется авторизация"}, status_code=401)
    session = main.db.get_onboarding_session(session_id, user["id"])
    if not session:
        return JSONResponse({"error": "Черновик не найден"}, status_code=404)
    
    status = session.get("status")
    res = {"ok": True, "status": status}
    
    if status == "completed" and session.get("generated_site_id"):
        site = main.db.get_site_by_id(session["generated_site_id"])
        if site and site["user_id"] == user["id"]:
            res["workspace_url"] = f"/dashboard/sites/{site['id']}"
    elif status == "failed":
        res["error"] = session.get("error") or "Не удалось создать сайт"
        
    return JSONResponse(res)
