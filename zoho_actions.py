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
  * keep a Vome teammate (Ron) copied when any client message on the ticket
    was addressed or copied to them, even if the newest one dropped them.
    The client put Ron on To, not CC, and the reply dropped him (#9610)
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
# Owner of a ticket the agent is fixing: Sam merges agent PRs into dev.
SAM_AGENT = os.environ.get("ZOHO_AGENT_SAM") or "569440000000139001"

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


_TEAM_DOMAINS = ("@vomevolunteer.com", "@vomevolunteer.co")
# Shared inboxes, and Sam, who answers from the desk and sees every reply.
_NOT_TEAMMATES = {
    "admin@vomevolunteer.com", "sam@vomevolunteer.com",
    "s.fagen@vomevolunteer.com", "s.fagen@vomevolunteer.co",
}


def teammates_copied(threads: list[dict]) -> list[str]:
    """Vome teammates a message on the ticket was addressed or copied to.
    Forwards (a ticket sent on to an engineer) do not count."""
    found: list[str] = []
    for t in threads:
        if t.get("isForward"):
            continue
        for address in emails_in(t.get("to")) + emails_in(t.get("cc")):
            if (address.endswith(_TEAM_DOMAINS) and not _ours(address)
                    and address not in _NOT_TEAMMATES
                    and address not in found):
                found.append(address)
    return found


def ticket_teammates(ticket_id: str) -> list[str]:
    """teammates_copied for one ticket, [] if the thread cannot be read."""
    try:
        return teammates_copied(_threads(ticket_id))
    except Exception:
        return []


def first_name(author: str, email: str = "", given: str = "") -> str:
    """"Phan, Dalena" -> Dalena. "Stephanie Sielaty" -> Stephanie.

    `given` is the first name Zoho keeps on the author, and wins when set:
    the display name cannot say where a two-word first name ends ("Ia Faye
    Sison" was greeted as "Ia", #9596).
    """
    given = (given or "").strip()
    if given and "@" not in given:
        return given
    author = (author or "").strip().strip('"')
    if "," in author:
        after = author.split(",", 1)[1].strip()
        if after:
            return after.split()[0]
    if author and "@" not in author:
        return author.split()[0]
    local = (email or "").split("@")[0]
    return re.split(r"[._\d]", local)[0].capitalize() if local else ""


def _newest(threads: list[dict], direction: str, skip=None) -> dict:
    found = [t for t in threads if t.get("direction") == direction
             and not (skip and skip(t))]
    if not found:
        return {}
    found.sort(key=lambda t: str(t.get("createdTime") or ""), reverse=True)
    return found[0]


def _threads(ticket_id: str) -> list[dict]:
    return _get(f"/tickets/{ticket_id}/threads", {"limit": 50}).get(
        "data") or []


def latest_inbound(ticket_id: str) -> dict:
    """The newest message from the customer side, or {} if none."""
    return _newest(_threads(ticket_id), "in")


def latest_outbound(ticket_id: str) -> dict:
    """The newest message we sent on this ticket, or {} if none."""
    return _newest(_threads(ticket_id), "out")


def last_exchange(ticket_id: str, skip=None) -> tuple[dict, dict]:
    """(newest inbound, newest outbound) from one read of the thread.
    `skip(thread)` true leaves an inbound out (an out-of-office)."""
    threads = _threads(ticket_id)
    return _newest(threads, "in", skip), _newest(threads, "out")


def ticket_status(ticket_id: str) -> str:
    """The ticket's Zoho status ("Closed", "Processing", ...)."""
    return str(_get(f"/tickets/{ticket_id}").get("status") or "")


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
    threads = _threads(ticket_id)
    latest = _newest(threads, "in")
    # Web-form tickets (channel WEB) carry no fromEmailAddress; the address
    # is only on the author.
    sender = (emails_in(latest.get("fromEmailAddress"))
              or emails_in((latest.get("author") or {}).get("email")))
    to = sender[0] if sender else ""
    author = (latest.get("author") or {}).get("name") or ""
    given = (latest.get("author") or {}).get("firstName") or ""
    cc: list[str] = []
    for address in (emails_in(latest.get("cc")) + _secondary_emails(ticket_id)
                    + teammates_copied(threads)):
        if address != to and not _ours(address) and address not in cc:
            cc.append(address)
    return {
        "to": to,
        "cc": cc,
        "first_name": first_name(author, to, given),
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


def follow_route(ticket_id: str, team: str) -> bool:
    """A ticket owned by one team, routed to the other: move the owner.

    #9501: acknowledged with Backend Team as owner, reopened by the client,
    routed to Sanjay, and Zoho still showed Backend Team. Only the owner
    moves, never the status (New or Processing still means the client is
    owed Sam's reply). A ticket owned by anyone other than the other team
    (Support Agent, Sam) is left for `set_in_progress` when the ack is sent.
    True when the owner was moved.
    """
    owner = FRONTEND_TEAM if team == "frontend" else BACKEND_TEAM
    other = BACKEND_TEAM if owner == FRONTEND_TEAM else FRONTEND_TEAM
    if str(_get(f"/tickets/{ticket_id}").get("assigneeId") or "") != other:
        return False
    _send("PATCH", f"/tickets/{ticket_id}", {"assigneeId": owner})
    return True


AGENT_FIX_TAG = "agent-fix"


def mark_agent_fix(ticket_id: str, pr_url: str, task_url: str = "") -> str:
    """The agent opened a fix PR: show it in Zoho, without emailing anyone.

    Tagged `agent-fix`, with a private note linking the PR. The status is
    left alone. Sam's rule (2026-10-19): New or Processing in Zoho means
    "this client is owed a reply", so nothing the agents do on their own
    moves a ticket out of it. `set_agent_fixing` moves it once Sam's
    acknowledgment is sent (`sprint_thread._after_send`). Each part
    fails on its own and is reported, never raised: the PR already exists.
    """
    notes = []
    steps = (
        ("tag", lambda: _send("POST", f"/tickets/{ticket_id}/associateTag",
                              {"tags": [AGENT_FIX_TAG]})),
        ("note", lambda: _send("POST", f"/tickets/{ticket_id}/comments", {
            "isPublic": False, "contentType": "plainText",
            "content": ("Vome OS: the agent opened a fix for this ticket "
                        f"and owns it until it is live.\nFix PR: {pr_url}"
                        + (f"\nAgent Fixes task: {task_url}"
                           if task_url else "")),
        })),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:
            notes.append(f"Zoho {name} not set ({str(exc)[:120]})")
    return "; ".join(notes)


def set_agent_fixing(ticket_id: str) -> dict:
    """The client has been told and the agent's fix is in flight: In
    Progress, assigned to Sam because his merge is the next human step."""
    return _send("PATCH", f"/tickets/{ticket_id}",
                 {"status": "In Progress", "assigneeId": SAM_AGENT})


def unmark_agent_fix(ticket_id: str, pr_url: str) -> str:
    """The fix PR was closed unmerged: nobody is fixing this any more.

    Back to Processing (needs attention) with the tag removed and a note
    saying why, so Zoho does not keep claiming the agent has it.
    """
    notes = []
    steps = (
        ("status", lambda: _send("PATCH", f"/tickets/{ticket_id}",
                                 {"status": "Processing"})),
        ("tag", lambda: _send("POST",
                              f"/tickets/{ticket_id}/dissociateTag",
                              {"tags": [AGENT_FIX_TAG]})),
        ("note", lambda: _send("POST", f"/tickets/{ticket_id}/comments", {
            "isPublic": False, "contentType": "plainText",
            "content": ("Vome OS: the agent's fix PR was closed without "
                        f"merging ({pr_url}). Nobody is fixing this now; "
                        "it is back in the sprint for a decision."),
        })),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:
            notes.append(f"Zoho {name} not reset ({str(exc)[:120]})")
    return "; ".join(notes)


def ticket_url(ticket_id: str) -> str:
    return ("https://support.vomevolunteer.com/support/vomevolunteer/"
            f"ShowHomePage.do#Cases/dv/{ticket_id}")
