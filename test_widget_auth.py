"""The widget routes answer only Django, and the routes nothing calls are
ops-only (main.py). Before this, anyone who found the server could chat as
any email, read anyone's tickets and activate any account."""

import os

os.environ.setdefault("VOMEOS_BROKER_URL", "memory://")  # no scheduler

import pytest
from starlette.testclient import TestClient

import main

KEY = "test-support-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPPORT_API_KEY", KEY)
    monkeypatch.setenv("OPS_TOKEN", "test-ops-token")
    import zoho_desk_api

    monkeypatch.setattr(
        zoho_desk_api, "search_tickets", lambda email: [{"id": "1"}])
    return TestClient(main.app)


def test_tickets_refused_without_key_when_enforcing(client, monkeypatch):
    monkeypatch.setenv("CHAT_REQUIRE_API_KEY", "1")
    r = client.get("/chat/tickets", params={"email": "anyone@org.ca"})
    assert r.status_code == 401
    r = client.get(
        "/chat/tickets", params={"email": "anyone@org.ca"},
        headers={"X-Support-Api-Key": "wrong"})
    assert r.status_code == 401


def test_tickets_served_with_key_when_enforcing(client, monkeypatch):
    monkeypatch.setenv("CHAT_REQUIRE_API_KEY", "1")
    r = client.get(
        "/chat/tickets", params={"email": "anyone@org.ca"},
        headers={"X-Support-Api-Key": KEY})
    assert r.status_code == 200
    assert r.json() == {"tickets": [{"id": "1"}]}


def test_intake_refused_without_key_when_enforcing(client, monkeypatch):
    monkeypatch.setenv("CHAT_REQUIRE_API_KEY", "1")
    monkeypatch.setattr(
        main, "run_intake_turn",
        lambda **kw: pytest.fail("intake must not run"))
    r = client.post("/chat/intake", json={"message": "hi"})
    assert r.status_code == 401


def test_log_only_mode_still_serves(client, monkeypatch):
    monkeypatch.delenv("CHAT_REQUIRE_API_KEY", raising=False)
    r = client.get("/chat/tickets", params={"email": "anyone@org.ca"})
    assert r.status_code == 200


@pytest.mark.parametrize("method,path", [
    ("get", "/chat/auth-check?email=a@b.ca"),
    ("post", "/chat/auth-bypass"),
    ("get", "/debug/test-ticket-fetch"),
    ("get", "/debug/mcp-tools"),
])
def test_unused_routes_are_ops_only(client, method, path):
    kwargs = {"json": {"email": "a@b.ca"}} if method == "post" else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code in (401, 422)
    r = getattr(client, method)(
        path, headers={"Authorization": "Bearer wrong"}, **kwargs)
    assert r.status_code == 401
