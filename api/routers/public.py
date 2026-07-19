from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from core.templating import templates

router = APIRouter(tags=["public"])

@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
        user = getattr(request.state, "user", None)
        return templates.TemplateResponse(request, "landing.html", {"error": "", "user": user})

@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(request, "terms.html", {"user": user})
