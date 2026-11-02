"""The Desk MCP server's {"data", "headers", "status"} envelope (2026-11-02)."""
import json

from agent import _extract_ticket_fields, _unwrap_mcp_result


def _wrap(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_ticket_inside_envelope_reads_its_own_status():
    ticket = {"id": "", "subject": "Kiosk down", "status": "Open",
              "contact": {"email": "a@b.org"}}
    raw = _wrap({"data": ticket, "status": "success",
                 "headers": {"content-type": "application/json"}})
    fields = _extract_ticket_fields(raw)
    assert fields["status"] == "Open"
    assert fields["subject"] == "Kiosk down"
    assert fields["contact_email"] == "a@b.org"


def test_conversations_inside_envelope_keep_their_list():
    raw = _wrap({"data": {"data": [{"id": "1", "type": "thread"}]},
                 "status": "success", "headers": {}})
    assert _unwrap_mcp_result(raw) == {"data": [{"id": "1", "type": "thread"}]}


def test_old_shape_unchanged():
    ticket = {"subject": "x", "status": "Open"}
    assert _unwrap_mcp_result(_wrap(ticket)) == ticket
