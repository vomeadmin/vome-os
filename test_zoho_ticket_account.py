"""A widget ticket is not lost when Zoho refuses the account (#9762).

From Nov 3 to Nov 5 2026 every widget ticket failed with a 422: the
account found by org name was not the one linked to the existing contact.
"""

import zoho_desk_api as z


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


REFUSED = {"errorCode": "INVALID_DATA", "errors": [{
    "fieldName": "/accountId", "errorType": "invalid",
    "errorMessage": "You cannot perform this action because the account "
                    "is not associated with this contact."}]}


def _patch(monkeypatch, responses):
    sent = []

    def fake_request(method, path, json_body=None, params=None, **kw):
        sent.append(dict(json_body or {}))
        return responses.pop(0)

    monkeypatch.setattr(z, "_api_request", fake_request)
    monkeypatch.setattr(z, "ZOHO_DESK_CLIENT_ID", "x")
    monkeypatch.setattr(z, "ZOHO_DESK_CLIENT_SECRET", "x")
    monkeypatch.setattr(z, "ZOHO_DESK_REFRESH_TOKEN", "x")
    return sent


def test_retries_without_account_when_zoho_refuses_it(monkeypatch):
    sent = _patch(monkeypatch, [
        _Resp(422, REFUSED),
        _Resp(200, {"id": "t1", "ticketNumber": "9900"}),
    ])
    out = z.create_ticket("S", "D", "a@org.ca", contact_id="c1", account_id="a1")
    assert out["id"] == "t1"
    assert sent[0]["accountId"] == "a1"
    assert "accountId" not in sent[1]
    assert sent[1]["contactId"] == "c1"


def test_other_422_is_not_retried(monkeypatch):
    other = {"errorCode": "INVALID_DATA", "errors": [{"fieldName": "/email"}]}
    sent = _patch(monkeypatch, [_Resp(422, other)])
    assert z.create_ticket("S", "D", "a@org.ca", contact_id="c1", account_id="a1") is None
    assert len(sent) == 1


def test_no_account_sent_means_no_retry(monkeypatch):
    sent = _patch(monkeypatch, [_Resp(422, REFUSED)])
    assert z.create_ticket("S", "D", "a@org.ca", contact_id="c1") is None
    assert len(sent) == 1
