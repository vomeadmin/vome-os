"""Vic gets two VomeOS lookups, rebuilt from an allowlist of fields, so
internal detail staff see through the MCP never reaches a client
(vic_tools.py)."""

import json

from mcp_connector import tools as mcp_tools

import vic_tools

RAW_LOOKUP = {
    "question": "approve hours",
    "help_centre": [{
        "title": "Approve hour requests",
        "url": "https://support.vomevolunteer.com/kb/approve-hours",
        "excerpt": "Go to Hours, then click Approve all.",
        "days_since_updated": 12,
    }],
    "setup_guide_and_catalog": [
        {"source": "setup_guide", "section": "Hours",
         "text": "Approve hours from the Hours page."},
        {"source": "feature_catalog", "section": "hero",
         "text": "HIPAA, GDPR & SOC 2"},
    ],
    "ui_text": [
        {"surface": "web", "kind": "label", "text": "Approve all",
         "internal_key": "hours_approve_all_btn"},
        {"surface": "web", "kind": "label",
         "text": "Data is encrypted in transit",
         "internal_key": "security_banner_text"},
    ],
    "caveats": [{"lesson": "Bulk approve skips flagged rows (bug 868abc)"}],
    "sources": {"ui_text": "searched 9000 strings on branch main"},
    "how_to_use_this": "INTERNAL CONTEXT...",
}

RAW_WHERE = {
    "question": "approve hours",
    "pages": [{
        "route": "/org/hours",
        "area": "org > dashboard > hours",
        "internal_file": "src/views/org/dashboard/hours/HourRequests.js",
        "matched_text": ["Approve all"],
        "text_on_this_screen": [
            {"text": "Hour requests", "kind": "label"},
            {"text": "Tokens expire after 24 hours", "kind": "explanation"},
        ],
    }],
    "sources": {"navigation": "searched 300 routes on branch main"},
}


def test_only_two_tools_offered():
    assert {t["name"] for t in vic_tools.TOOLS} == {
        "lookup_feature", "where_in_app"}


def test_unknown_tool_gets_nothing():
    for name in ["search_code", "read_code_file", "check_existing_work",
                 "send_ticket_reply"]:
        assert json.loads(vic_tools.run_tool(name, {})) == {
            "error": "unknown tool"}


def test_lookup_keeps_only_client_safe_fields(monkeypatch):
    seen = {}

    def fake(question, language="en", wait_for_source=True):
        seen["wait"] = wait_for_source
        return RAW_LOOKUP

    monkeypatch.setattr(mcp_tools, "lookup_feature", fake)
    out = vic_tools.run_tool("lookup_feature", {"question": "approve hours"})
    data = json.loads(out)

    assert seen["wait"] is False
    assert data["help_centre"][0]["title"] == "Approve hour requests"
    assert data["setup_guide"] == [
        {"section": "Hours", "text": "Approve hours from the Hours page."}]
    assert data["screen_text"] == ["Approve all"]
    for leaked in ["internal_key", "hours_approve_all_btn", "caveat",
                   "868abc", "branch", "SOC 2", "encrypted",
                   "INTERNAL CONTEXT", "days_since_updated"]:
        assert leaked not in out, leaked


def test_where_keeps_only_screen_words(monkeypatch):
    seen = {}

    def fake(question, wait_for_source=True):
        seen["wait"] = wait_for_source
        return RAW_WHERE

    monkeypatch.setattr(mcp_tools, "where_in_app", fake)
    out = vic_tools.run_tool("where_in_app", {"question": "approve hours"})
    data = json.loads(out)

    assert seen["wait"] is False
    assert data["pages"] == [
        {"text_on_this_page": ["Approve all", "Hour requests"]}]
    for leaked in ["src/", ".js", "internal_file", "org > dashboard",
                   "/org/hours", "Tokens expire", "branch"]:
        assert leaked not in out, leaked


def test_failure_does_not_pass_the_exception_on(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("could not open /app/src/views/secret.js on db-1")

    monkeypatch.setattr(mcp_tools, "lookup_feature", boom)
    out = vic_tools.run_tool("lookup_feature", {"question": "x"})
    assert json.loads(out)["error"] == "lookup unavailable"
    assert "secret" not in out and "db-1" not in out


def test_mcp_tools_still_wait_by_default(monkeypatch):
    """Staff through the MCP keep the full, freshly fetched answer."""
    from mcp_connector import product_source

    calls = []
    monkeypatch.setattr(
        product_source, "ensure_fresh", lambda: calls.append("wait") or {})
    monkeypatch.setattr(
        product_source, "refresh_in_background",
        lambda: calls.append("background") or {})
    assert mcp_tools._source_state(True) == {}
    assert mcp_tools._source_state(False) == {}
    assert calls == ["wait", "background"]
