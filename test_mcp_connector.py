"""
test_mcp_connector.py

The claude.ai connector, end to end: sign-in, tokens, the dispatcher in
front of FastAPI, and the tools' refusal and dedupe behaviour.

Google is the only thing faked. Everything between claude.ai and a tool
result runs for real against an in-memory SQLite store.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mcp_connector import (
    config,
    feature_requests,
    knowledge,
    oauth,
    product_source,
    store,
    tools,
)

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
SAM = "s.fagen@vomevolunteer.co"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://testserver")
    monkeypatch.setenv("MCP_GOOGLE_CLIENT_ID", "google-client")
    monkeypatch.setenv("MCP_GOOGLE_CLIENT_SECRET", "google-secret")
    monkeypatch.delenv("MCP_ALLOWED_DOMAINS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("MCP_TRIAGE_EMAILS", raising=False)
    store.set_engine(create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    ))
    monkeypatch.setattr(product_source, "ensure_fresh", lambda force=False: {
        "mode": "test", "web_branch": None, "error": None,
    })


# ---------------------------------------------------------------------------
# who gets in
# ---------------------------------------------------------------------------

def test_company_domains_are_allowed_and_others_are_not():
    assert config.email_allowed("Someone@VomeVolunteer.com")
    assert config.email_allowed("someone@vomevolunteer.co")
    assert not config.email_allowed("someone@gmail.com")
    assert not config.email_allowed("vomevolunteer.co")
    assert not config.email_allowed("")


def test_individual_addresses_can_be_added(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "contractor@example.org")
    assert config.email_allowed("contractor@example.org")
    assert not config.email_allowed("other@example.org")


def test_only_sam_triages_by_default():
    assert config.can_triage(SAM)
    assert not config.can_triage("colleague@vomevolunteer.co")
    assert config.clickup_user_for(SAM) == 3691763
    assert config.clickup_user_for("colleague@vomevolunteer.co") is None


def test_connector_is_off_without_google(monkeypatch):
    monkeypatch.delenv("MCP_GOOGLE_CLIENT_SECRET")
    assert not config.enabled()


@pytest.mark.parametrize("uri,ok", [
    (CLAUDE_CALLBACK, True),
    ("https://claude.com/api/mcp/auth_callback", True),
    ("http://localhost:33418/callback", True),
    ("http://127.0.0.1:5000/cb", True),
    ("http://claude.ai/api/mcp/auth_callback", False),
    ("https://claude.ai.attacker.example/cb", False),
    ("https://evil.example/cb", False),
])
def test_only_known_redirect_hosts_may_register(uri, ok):
    assert oauth.redirect_permitted(uri) is ok


def _claims(**over):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "google-client",
        "exp": time.time() + 300,
        "email": "Colleague@VomeVolunteer.co",
        "email_verified": True,
    }
    claims.update(over)
    return claims


def test_google_claims_are_checked():
    assert oauth.check_google_claims(_claims()) == "colleague@vomevolunteer.co"
    assert oauth.check_google_claims(_claims(aud="someone-else")) == ""
    assert oauth.check_google_claims(_claims(iss="https://evil")) == ""
    assert oauth.check_google_claims(_claims(exp=time.time() - 1)) == ""
    assert oauth.check_google_claims(_claims(email_verified=False)) == ""


def test_id_token_payload_decodes():
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": "a@b.c"}).encode()
    ).decode().rstrip("=")
    assert oauth.decode_id_token(f"h.{payload}.s") == {"email": "a@b.c"}
    assert oauth.decode_id_token("garbage") == {}


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def test_a_code_can_be_redeemed_once():
    store.put_code("abc", {"subject": SAM})
    assert store.peek_code("abc") == {"subject": SAM}
    assert store.take_code("abc") == {"subject": SAM}
    assert store.take_code("abc") is None


def test_expired_codes_are_refused():
    store.put_code("old", {"subject": SAM}, ttl=-1)
    assert store.peek_code("old") is None
    assert store.take_code("old") is None


def test_tokens_are_stored_hashed_and_revoked_as_a_family():
    common = dict(client_id="c", subject=SAM, scopes=["vome:read"],
                  resource="r", family="fam")
    store.save_token("access-1", kind="access",
                     expires_at=time.time() + 60, **common)
    store.save_token("refresh-1", kind="refresh",
                     expires_at=time.time() + 60, **common)
    with store._get_engine().connect() as conn:
        raw = [r[0] for r in conn.execute(
            store.tokens.select().with_only_columns(store.tokens.c.token_hash)
        )]
    assert "access-1" not in raw and store.hash_secret("access-1") in raw

    assert store.load_token("access-1", "access")["subject"] == SAM
    assert store.load_token("access-1", "refresh") is None
    store.revoke_token("refresh-1")
    assert store.load_token("access-1", "access") is None


# ---------------------------------------------------------------------------
# the whole flow, as claude.ai drives it
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from mcp_connector import server

    signed_in = {"email": "colleague@vomevolunteer.co"}

    async def fake_google(self, code):
        return signed_in["email"]

    monkeypatch.setattr(oauth.GoogleOAuthProvider, "_google_email",
                        fake_google)

    app = FastAPI(lifespan=server.lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    assert server.attach(app)
    with TestClient(app, base_url="https://testserver") as test_client:
        test_client.signed_in = signed_in
        yield test_client


def _sign_in(client) -> dict:
    registered = client.post("/register", json={
        "redirect_uris": [CLAUDE_CALLBACK],
        "client_name": "Claude",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    to_google = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "claude-state",
        "resource": "https://testserver/mcp",
    }, follow_redirects=False)
    assert to_google.status_code == 302, to_google.text
    google_url = urlparse(to_google.headers["location"])
    assert google_url.netloc == "accounts.google.com"
    txn = parse_qs(google_url.query)["state"][0]

    back = client.get("/oauth/google/callback",
                      params={"state": txn, "code": "google-code"},
                      follow_redirects=False)
    assert back.status_code == 302
    to_claude = urlparse(back.headers["location"])
    query = parse_qs(to_claude.query)
    assert to_claude.netloc == "claude.ai"
    assert query["state"] == ["claude-state"]
    if "error" in query:
        return {"error": query["error"][0]}

    tokens = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": "https://testserver/mcp",
    })
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    body["client_id"] = client_id
    body["code"] = query["code"][0]
    body["verifier"] = verifier
    return body


def _rpc(client, token, method, params=None, rpc_id=1):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method,
              "params": params or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    return response


def _initialize(client, token):
    return _rpc(client, token, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })


def test_discovery_metadata_points_at_this_server(client):
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == "https://testserver/mcp"
    assert resource["authorization_servers"] == ["https://testserver/"] or \
        resource["authorization_servers"] == ["https://testserver"]
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["registration_endpoint"].endswith("/register")
    assert server["code_challenge_methods_supported"] == ["S256"]


def test_mcp_refuses_without_a_token(client):
    response = client.post("/mcp", json={})
    assert response.status_code == 401
    assert "oauth-protected-resource" in response.headers.get(
        "www-authenticate", ""
    )


def test_other_routes_still_reach_fastapi(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_a_stranger_cannot_register_a_redirect(client):
    response = client.post("/register", json={
        "redirect_uris": ["https://evil.example/cb"],
        "token_endpoint_auth_method": "none",
    })
    assert response.status_code == 400


def test_full_sign_in_then_tools(client):
    tokens = _sign_in(client)
    assert tokens["access_token"] and tokens["refresh_token"]

    init = _initialize(client, tokens["access_token"])
    assert init.status_code == 200, init.text

    listed = _rpc(client, tokens["access_token"], "tools/list", rpc_id=2)
    assert listed.status_code == 200, listed.text
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {
        "lookup_feature", "where_in_app", "check_existing_work",
        "search_code", "read_code_file", "list_repositories",
        "log_feature_request", "add_request_to_existing",
        "triage_feature_request", "connector_status",
    } <= names

    status = _rpc(client, tokens["access_token"], "tools/call", {
        "name": "connector_status", "arguments": {},
    }, rpc_id=3)
    result = status.json()["result"]
    assert not result.get("isError"), result
    payload = result.get("structuredContent") or json.loads(
        result["content"][0]["text"]
    )
    payload = payload.get("result", payload)
    assert payload["signed_in_as"] == "colleague@vomevolunteer.co"
    assert payload["can_triage_feature_requests"] is False

    # Every call is audited with who made it.
    calls = store.recent_calls()
    assert calls[0]["tool"] == "connector_status"
    assert calls[0]["subject"] == "colleague@vomevolunteer.co"


def test_a_code_cannot_be_replayed(client):
    tokens = _sign_in(client)
    replay = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": tokens["code"],
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": tokens["client_id"],
        "code_verifier": tokens["verifier"],
    })
    assert replay.status_code == 400


def test_refresh_rotates(client):
    tokens = _sign_in(client)
    refreshed = client.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"],
    })
    assert refreshed.status_code == 200, refreshed.text
    again = client.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"],
    })
    assert again.status_code == 400


def test_outside_email_is_sent_back_with_access_denied(client):
    client.signed_in["email"] = "someone@gmail.com"
    assert _sign_in(client) == {"error": "access_denied"}


def test_removing_someone_cuts_them_off_on_the_next_call(client, monkeypatch):
    tokens = _sign_in(client)
    assert _initialize(client, tokens["access_token"]).status_code == 200
    monkeypatch.setenv("MCP_ALLOWED_DOMAINS", "example.org")
    assert _initialize(client, tokens["access_token"]).status_code == 401


def test_a_colleague_cannot_triage(client, monkeypatch):
    called = []
    monkeypatch.setattr(feature_requests, "triage",
                        lambda **kw: called.append(kw))
    tokens = _sign_in(client)
    _initialize(client, tokens["access_token"])
    response = _rpc(client, tokens["access_token"], "tools/call", {
        "name": "triage_feature_request",
        "arguments": {"task_id": "abc123", "decision": "declined"},
    }, rpc_id=4)
    result = response.json()["result"]
    assert result["isError"] is True
    assert "cannot triage" in result["content"][0]["text"]
    assert called == []


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def test_existing_work_reports_not_searched_without_a_token(monkeypatch):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    result = tools.check_existing_work("kiosk timezone")
    assert result["ran"] is False
    assert "UNKNOWN" in result["how_to_use_this"]


def test_existing_work_failure_is_not_an_empty_result(monkeypatch):
    import clickup_search

    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")

    def boom(*a, **k):
        raise RuntimeError("ClickUp down")

    monkeypatch.setattr(clickup_search, "search_tasks_by_text", boom)
    result = tools.check_existing_work("kiosk timezone")
    assert result["ran"] is False
    assert "NOT searched" in result["sources"]["clickup"]


def test_lookup_names_every_source_and_never_hides_one(monkeypatch):
    monkeypatch.setattr(tools, "_db_usable", lambda: False)
    monkeypatch.setattr(knowledge, "search_caveats", lambda q: {
        "sources": {"sprint_lessons_log": "NOT searched (test)"},
        "caveats": [],
    })
    result = tools.lookup_feature("export volunteers to excel")
    assert result["sources"]["help_centre"].startswith("NOT searched")
    assert "sprint_lessons_log" in result["sources"]
    assert "ui_text" in result["sources"]
    assert "Never put a file path" in result["how_to_use_this"]


def test_duplicates_block_creation_until_confirmed(monkeypatch):
    created = []
    monkeypatch.setattr(feature_requests, "find_duplicates", lambda t, p: [{
        "id": "86x", "name": "Holiday-aware recurring shifts",
        "list": "Raw Intake", "status": "queued", "shipped": False,
        "url": "https://app.clickup.com/t/86x",
    }])
    monkeypatch.setattr(feature_requests, "create_request",
                        lambda **kw: created.append(kw) or {"task_id": "new"})
    args = dict(title="Recurring shifts skip holidays", problem="p",
                requested_by="Riverbend Parks Alliance", logged_by=SAM)

    first = tools.log_feature_request(**args)
    assert first["created"] is False
    assert first["possible_duplicates"][0]["task_id"] == "86x"
    assert created == []

    second = tools.log_feature_request(**args, confirm_new=True)
    assert second["created"] is True
    assert "Not that it is planned" in second["what_the_customer_may_be_told"]


def test_dedupe_failure_creates_nothing(monkeypatch):
    def boom(t, p):
        raise RuntimeError("ClickUp down")

    monkeypatch.setattr(feature_requests, "find_duplicates", boom)
    with pytest.raises(feature_requests.RequestError, match="nothing was"):
        tools.log_feature_request(
            title="Recurring shifts skip holidays", problem="p",
            requested_by="Org", logged_by=SAM,
        )


def test_writes_refuse_tasks_outside_the_backlog(monkeypatch):
    import httpx

    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "abc123", "list": {"id": "999999"}}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    with pytest.raises(feature_requests.RequestError, match="not in a"):
        feature_requests.add_plus_one(
            task_id="abc123", requested_by="Org", logged_by=SAM,
        )


def test_code_tools_refuse_unknown_repos_and_traversal():
    unknown = tools.read_code_file("nope", "a.py")
    assert "unknown repository" in unknown["error"]
    assert "path inside" in tools.read_code_file("core", "../etc/passwd")[
        "error"
    ]


def test_live_branches_come_from_the_confirmed_table():
    from mcp_connector import repos

    assert repos.resolve("web").live_ref == "master"
    assert repos.resolve("core").slug == "vomedjango-restored-core-app"
    assert repos.resolve("chats").host == "bitbucket"


def test_markdown_chunks_keep_their_heading():
    chunks = knowledge.chunk_markdown(
        "## Hour claims\n\nThe Actions option only appears when a claim "
        "exists.\n\n## Kiosk\n\nPIN resets need an admin.",
        "Lessons",
    )
    assert chunks[0] == (
        "Lessons: Hour claims",
        "The Actions option only appears when a claim exists.",
    )
    assert chunks[1][0] == "Lessons: Kiosk"


# ---------------------------------------------------------------------------
# the frontend source on a box with no checkout
# ---------------------------------------------------------------------------

def _tarball(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_download_keeps_only_what_the_product_modules_read(
    monkeypatch, tmp_path,
):
    import contextlib

    import httpx

    from mcp_connector import repos

    archive = _tarball({
        "vomeadmin-vome-react-abc/src/translations/postlogin.js": b"en: {}",
        "vomeadmin-vome-react-abc/src/routers/OrgRoutes.js": b"<Route />",
        "vomeadmin-vome-react-abc/src/views/org/Page.js": b"t('x')",
        "vomeadmin-vome-react-abc/src/views/org/logo.png": b"png",
        "vomeadmin-vome-react-abc/src/api/secrets.js": b"nope",
        "vomeadmin-vome-react-abc/src/views/../../../escape.js": b"nope",
    })
    requested = {}

    class Stream:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield archive

    @contextlib.contextmanager
    def fake_stream(method, url, **kwargs):
        requested["url"] = url
        yield Stream()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    monkeypatch.setattr(product_source, "_github_token", lambda owner: "t")

    dest = tmp_path / "vome-react"
    product_source._download(
        repos.resolve("web"), product_source._WEB_KEEP, dest,
    )
    kept = sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    )
    assert kept == [
        "src/routers/OrgRoutes.js",
        "src/translations/postlogin.js",
        "src/views/org/Page.js",
    ]
    assert requested["url"].endswith("/vomeadmin/vome-react/tarball/master")
    assert not (tmp_path / "escape.js").exists()


def test_single_quoted_strings_are_read(tmp_path, monkeypatch):
    import product_strings

    translations = tmp_path / "translations"
    translations.mkdir()
    (translations / "postlogin.js").write_text(
        "export default {\n"
        "  en: {\n"
        "    approve_all: 'Approve all',\n"
        '    export_excel: "Export to Excel",\n'
        r"    it_s: 'It\'s here'," "\n"
        "  },\n"
        "  fr: {\n"
        "    approve_all: 'Approuver tous',\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VOME_REACT_PATH", str(translations))
    monkeypatch.setenv("VOME_APP_PATH", str(tmp_path / "missing"))
    product_strings._strings.cache_clear()
    try:
        found = {key: value for _, key, value in product_strings._strings()}
    finally:
        product_strings._strings.cache_clear()
    assert found == {
        "approve_all": "Approve all",
        "export_excel": "Export to Excel",
        # Escapes are kept as written; the reader is a model, not a JS
        # engine, and "It\'s" reads fine.
        "it_s": r"It\'s here",
    }
