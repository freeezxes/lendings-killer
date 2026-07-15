"""Authenticated dashboard rendering.

Guards the dict->ORM refactor breakages: the `services` package shadowing,
missing `dashboard_view`, glued route decorators, and User dict-compat. Each
of these 500'd a logged-in page in production.
"""
import sqlite3
import uuid
from datetime import datetime

import pytest_asyncio

from tests.conftest import _TMP_DB


@pytest_asyncio.fixture
async def logged_in_sid():
    # Insert a user + a valid session directly, return the session id.
    conn = sqlite3.connect(_TMP_DB)
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM users WHERE id = 1")
    conn.execute(
        "INSERT INTO users (id, phone, name, tokens, site_slots) VALUES (1,'77000000001','QA',10,1)"
    )
    sid = uuid.uuid4().hex
    expires = datetime.utcnow().replace(year=datetime.utcnow().year + 1).isoformat()
    conn.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, 1, expires))
    conn.commit()
    conn.close()
    return sid


DASHBOARD_PAGES = [
    "/dashboard",
    "/dashboard/billing",
    "/dashboard/create",
    "/profile",
    "/payment?reason=welcome",
]


async def test_dashboard_pages_render_for_logged_in_user(client, logged_in_sid):
    client.cookies.set("sid", logged_in_sid)
    for path in DASHBOARD_PAGES:
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code < 500, f"{path} -> {resp.status_code}\n{resp.text[:400]}"


async def test_profile_write_routes_are_registered(client):
    # The glued-decorator bug silently dropped these POST routes; a missing
    # route returns 404, a registered one returns anything else (auth/redirect).
    for path in ("/profile/update", "/profile/update-password"):
        resp = await client.post(path, data={}, follow_redirects=False)
        assert resp.status_code != 404, f"{path} not registered"
