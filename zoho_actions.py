"""
zoho_actions.py

The few things the sprint thread does to a Zoho ticket, and who a reply goes
to.

THIS IS THE ONLY MODULE IN THE SPRINT FLOW THAT CAN REACH A CUSTOMER
-------------------------------------------------------------------
`send_reply` emails a customer. It is called from exactly one place: Sam
asking for a send in the sprint thread, which sprint_thread checks against
his own words before calling it. Every send is logged in #vome-agent-log.

WHO A REPLY GOES TO
-------------------
From the playbook, learned the hard way:

  * reply to whoever wrote the LATEST customer message, not the ticket's
    contact record (the widget sets the volunteer as contact when the admin
    wrote in: #9236, #9434, #9446)
  * copy everyone that message copied, plus the ticket's secondary contacts.
    Zoho does not add secondary contacts on its own (Kimberly Crest, #9313)
  * never pull an address out of an earlier, forwarded part of the thread
  * never copy our own support mailbox back to itself
"""

from __future__ import annotations

import os
import re

import httpx

ZOHO_BASE = "https://desk.zoho.com/api/v1"
FROM_ADDRESS = os.environ.get(
    "ZOHO_FROM_ADDRESS", "support@vomevolunteer.zohodesk.com"
)

BACKEND_TEAM = os.environ.get("ZOHO_AGENT_BACKEND", "569440000023160001")
FRONTEND_TEAM = os.environ.get("ZOHO_AGENT_FRONTEND", "569440000023159001")

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_OUR_MAILBOXES = ("zohodesk.com", "support@vomevolunteer")


def _headers() -> dict[str, str]:
    from zoho_desk_api import ZOHO_ORG_ID, _get_token

    return {
        "Authorization": f"Zoho-oauthtoken {_get_token()}",
        "orgId": str(ZOHO_ORG_ID),
    }


def _get(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(ZOHO_BASE + path, headers=_headers(), params=params,
                     timeout=20)
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _send(method: str, path: str, body: dict) -> dict:
    resp = httpx.request(method, ZOHO_BASE + path, headers=_headers(),
                         json=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Zoho {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text.strip() else {}


def emails_in(value) -> list[str]:
    """Every address in a header value, list or string, lowercased."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    return [e.lower() for e in _EMAIL.findall(str(value or ""))]


def _ours(address: str) -> bool:
    return any(token in address for token in _OUR_MAILBOXES)


def first_name(author: str, email: str = "") -> str:
    """"Phan, Dalena" -> Dalena. "Stephanie Sielaty" -> Stephanie."""
    author = (author or "").strip().strip('"')
    if "," in author:
        after = author.split(",", 1)[1].strip()
        if after:
            return after.split()[0]
    if author and "@" not in author:
        return author.split()[0]
    local = (email or "").split("@")[0]
    return re.split(r"[._\d]", local)[0].capitalize() if local else ""


def latest_inbound(ticket_id: str) -> dict:
    """The newest message from the customer side, or {} if none."""
    data = _get(f"/tickets/{ticket_id}/threads", {"limit": 50})
    inbound = [t for t in (data.get("data") or [])
               if t.get("direction") == "in"]
    if not inbound:
        return {}
    inbound.sort(key=lambda t: str(t.get("createdTime") or ""), reverse=True)
    return inbound[0]


def _secondary_emails(ticket_id: str) -> list[str]:
    try:
        ticket = _get(f"/tickets/{ticket_id}")
    except Exception:
        return []
    found = []
    for contact_id in ticket.get("secondaryContacts") or []:
        try:
            contact = _get(f"/contacts/{contact_id}")
        except Exception:
            continue
        found += emails_in(contact.get("email"))
    return found


def recipients(ticket_id: str) -> dict:
    """{"to", "cc", "first_name", "thread_id"} for a reply to this ticket."""
    latest = latest_inbound(ticket_id)
    sender = emails_in(latest.get("fromEmailAddress"))
    to = sender[0] if sender else ""
    author = (latest.get("author") or {}).get("name") or ""
    cc: list[str] = []
    for address in emails_in(latest.get("cc")) + _secondary_emails(ticket_id):
        if address != to and not _ours(address) and address not in cc:
            cc.append(address)
    return {
        "to": to,
        "cc": cc,
        "first_name": first_name(author, to),
        "thread_id": str(latest.get("id") or ""),
        # When the customer last wrote: the age a batch shows.
        "received_at": str(latest.get("createdTime") or ""),
    }


def _reply_body(to: str, cc: list[str], content: str) -> dict:
    body = {
        "channel": "EMAIL",
        "fromEmailAddress": FROM_ADDRESS,
        "to": to,
        "content": content,
        "contentType": "plainText",
        "isForward": False,
    }
    if cc:
        body["cc"] = ",".join(cc)
    return body


def send_reply(ticket_id: str, to: str, cc: list[str], content: str) -> dict:
    """Email the customer. Only on Sam's send in the sprint thread."""
    if not to:
        raise ValueError("no recipient address on the latest message")
    return _send("POST", f"/tickets/{ticket_id}/sendReply",
                 _reply_body(to, cc, content))


def set_status(ticket_id: str, status: str) -> dict:
    """Set a ticket's status without Zoho emailing the customer about it.

    `disableClosureNotification` matters on Closed: without it Zoho sends its
    own "your ticket is closed" email, which is an unapproved client send
    (lessons log, 2026-09-28).
    """
    resp = httpx.request(
        "PATCH", f"{ZOHO_BASE}/tickets/{ticket_id}",
        headers=_headers(), params={"disableClosureNotification": "true"},
        json={"status": status}, timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Zoho {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text.strip() else {}


def set_in_progress(ticket_id: str, team: str) -> dict:
    """In Progress with the team as owner. Never left as Support Agent."""
    owner = FRONTEND_TEAM if team == "frontend" else BACKEND_TEAM
    return _send("PATCH", f"/tickets/{ticket_id}",
                 {"status": "In Progress", "assigneeId": owner})


def ticket_url(ticket_id: str) -> str:
    return ("https://support.vomevolunteer.com/support/vomevolunteer/"
            f"ShowHomePage.do#Cases/dv/{ticket_id}")
