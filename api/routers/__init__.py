from fastapi import APIRouter
from .auth import router as auth_router
from .dashboard import router as dashboard_router
from .sites import router as sites_router
from .admin import router as admin_router
from .chat import router as chat_router
from .public import router as public_router
from .payments import router as payments_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(dashboard_router)
api_router.include_router(sites_router)
api_router.include_router(admin_router)
api_router.include_router(chat_router)
api_router.include_router(public_router)
api_router.include_router(payments_router)
