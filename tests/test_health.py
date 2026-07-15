"""Smoke tests for basic app wiring: import, health probe, routing."""


def test_app_imports():
    import main

    assert main.app is not None
    # Sanity: a reasonable number of routes are registered.
    assert len(main.app.routes) > 10


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_landing_page_reachable(client):
    # Public landing page should render (or redirect), never 500.
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code < 500
