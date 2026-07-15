"""Shared pytest fixtures.

Each test run uses an isolated on-disk SQLite database so we never touch the
real ``lendings.db``. The env vars must be set before the app (and its settings
singleton) are imported, which is why they live at module top level.
"""
import os
import tempfile

# Point the app at a throwaway database + safe defaults before any app import.
_TMP_DB = os.path.join(tempfile.gettempdir(), "lendings_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB}")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_GUEST_LOGIN", "1")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import main


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
