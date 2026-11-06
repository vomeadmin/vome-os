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
# "Support Agent", Sam's own login: what it owns is Sam's core list.
SAM_AGENT = os.environ.get("ZOHO_AGENT_SAM") or "569440000000139001"
# The Zoho team that owns tickets the agent is fixing. The agent has no
# Zoho seat, so a team with no agent on it is its owner, and Sam's
# "Agent Fixes" view sits beside Backend and Frontend (2026-10-24).
AGENT_FIXES_TEAM = (os.environ.get("ZOHO_TEAM_AGENT_FIXES")
                    or "569440000043486190")

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
_SAM_ADDRESSES = {
    "sam@vomevolunteer.com",
    "s.fagen@vomevolunteer.com", "s.fagen@vomevolunteer.co",
}
# Shared inboxes, and Sam, who is copied only on customer threads (below).
_NOT_TEAMMATES = {"admin@vomevolunteer.com"} | _SAM_ADDRESSES
# Where Sam is copied when a thread is a customer conversation and no Sam
# address is on it already.
SAM_CC = (os.environ.get("SAM_CC_EMAIL") or "s.fagen@vomevolunteer.co").lower()


def is_team_address(address: str) -> bool:
    """A Vome person (Ron, Sam, an engineer), not a shared support mailbox."""
    address = (address or "").strip().lower()
    return address.endswith(_TEAM_DOMAINS) and not _ours(address)


def _sender(t: dict) -> str:
    # Web-form tickets (channel WEB) carry no fromEmailAddress; the address
    # is only on the author.
    found = (emails_in(t.get("fromEmailAddress"))
             or emails_in((t.get("author") or {}).get("email")))
    return found[0] if found else ""


def teammates_copied(threads: list[dict]) -> list[str]:
    """Vome teammates on the ticket: addressed or copied on a message, or
    the one who sent it in (Ron forwarding a customer's thread to support).
    Forwards we send out (a ticket sent on to an engineer) do not count."""
    found: list[str] = []
    for t in threads:
        if t.get("isForward"):
            continue
        addresses = emails_in(t.get("to")) + emails_in(t.get("cc"))
        if t.get("direction") == "in":
            addresses.append(_sender(t))
        for address in addresses:
            if (is_team_address(address)
                    and address not in _NOT_TEAMMATES
                    and address not in found):
                found.append(address)
    return found


def team_cc(threads: list[dict]) -> list[str]:
    """Vome people a reply on this ticket copies.

    Every teammate on the thread (teammates_copied), plus Sam when it is a
    customer conversation: a teammate is on it, or the client wrote to Sam
    himself. A plain inquiry (a volunteer or admin writing to support with
    nobody from Vome on it) copies no one (Sam, 2026-11-06).
    """
    found = teammates_copied(threads)
    sam_on_thread = ""
    for t in threads:
        if t.get("isForward"):
            continue
        addresses = emails_in(t.get("to")) + emails_in(t.get("cc"))
        if t.get("direction") == "in":
            addresses.append(_sender(t))
        sam_on_thread = next(
            (a for a in addresses if a in _SAM_ADDRESSES), "")
        if sam_on_thread:
            break
    if found or sam_on_thread:
        found.append(sam_on_thread or SAM_CC)
    return found


def ticket_teammates(ticket_id: str) -> list[str]:
    """team_cc for one ticket, [] if the thread cannot be read."""
    try:
        return team_cc(_threads(ticket_id))
    except Exception:
        return []


def first_name(author: str, email: str = "", given: str = "") -> str:
    """"Phan, Dalena" -> Dalena. "Stephanie Sielaty" -> Stephanie.

    `given` is the first name Zoho keeps on the author, and wins when set:
    the display name cannot say where a two-word first name ends ("Ia Faye
    Sison" was greeted as "Ia", #9596). A "Last, First" Outlook name can land
    whole in that field, so a comma sends it through the display-name rules
    ("Hi Phan, Dalena", #9521).
    """
    given = (given or "").strip().strip('"')
    if given and "@" not in given:
        if "," not in given:
            return given
        author = author or given
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


TRASHED = "Trashed"


def ticket_status(ticket_id: str) -> str:
    """The ticket's Zoho status ("Closed", "Processing", ...), or TRASHED
    for a ticket in the Recycle Bin. Zoho still answers 200 for those with
    their old status (#8194 read "In Progress" with isTrashed and
    isDeleted true), so without this they looked live (Sam, 2026-11-05:
    a deleted ticket always leaves the queue)."""
    ticket = _get(f"/tickets/{ticket_id}")
    if ticket.get("isTrashed") or ticket.get("isDeleted"):
        return TRASHED
    return str(ticket.get("status") or "")


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


_NAMED = re.compile(r'"?([^"<>,]*?)"?\s*<\s*([\w.+\'-]+@[\w-]+(?:\.[\w-]+)+)\s*>')
# The original sender in a forwarded message: "From: Heather <h@x.org>",
# Outlook's "From: Heather Smith <h@x.org>", French "De : ...".
_FORWARDED_FROM = re.compile(
    r"(?:^|\n)\s*\*?(?:From|De)\s*:\s*\*?\s*(.{0,120}?)"
    r"[<\[(]?(?:mailto:)?([\w.+'-]+@[\w-]+(?:\.[\w-]+)+)", re.I)


def _display_name(header, address: str) -> str:
    """The name a header gives an address ('"Heather S"<h@x.org>'), or ""."""
    for name, email in _NAMED.findall(str(header or "")):
        if email.lower() == address and "@" not in name:
            return name.strip()
    return ""


def _forwarded_sender(ticket_id: str, thread_id: str) -> tuple[str, str]:
    """(address, name) of the customer quoted in a teammate's forward."""
    if not thread_id:
        return "", ""
    try:
        thread = _get(f"/tickets/{ticket_id}/threads/{thread_id}")
    except Exception:
        return "", ""
    text = str(thread.get("content") or thread.get("summary") or "")
    text = re.sub(r"<br\s*/?>|</(?:p|div|tr|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text).replace("&lt;", "<").replace(
        "&gt;", ">").replace("&nbsp;", " ").replace("&quot;", '"')
    for name, address in _FORWARDED_FROM.findall(text):
        address = address.lower()
        if not is_team_address(address) and not _ours(address):
            return address, name.strip().strip('"<[(').strip()
    return "", ""


def _customer_for(ticket_id: str, threads: list[dict],
                  latest: dict) -> tuple[str, str, list[str]]:
    """(to, name, other clients) when a teammate sent the newest message.

    Ron forwards or copies a customer's thread to support so that we answer
    the customer, not him (2026-11-06, Heather's 2FA thread). In order:
    the clients his message was addressed or copied to, the newest client
    who wrote on the ticket themselves, then the sender his forward quotes.
    ("", "", []) when none is found, so nothing goes to Ron by mistake.
    """
    clients = [a for a in emails_in(latest.get("to")) + emails_in(
        latest.get("cc")) if not is_team_address(a) and not _ours(a)]
    if clients:
        to = clients[0]
        name = (_display_name(latest.get("to"), to)
                or _display_name(latest.get("cc"), to))
        return to, name, clients[1:]
    client_msg = _newest(threads, "in",
                         lambda t: is_team_address(_sender(t))
                         or _ours(_sender(t)) or not _sender(t))
    if client_msg:
        author = client_msg.get("author") or {}
        return (_sender(client_msg),
                author.get("firstName") or author.get("name") or "",
                [a for a in emails_in(client_msg.get("cc"))
                 if not is_team_address(a) and not _ours(a)])
    to, name = _forwarded_sender(ticket_id, str(latest.get("id") or ""))
    return to, name, []


def recipients(ticket_id: str) -> dict:
    """{"to", "cc", "first_name", "thread_id"} for a reply to this ticket.

    `forwarded_by` is set when a teammate sent the newest message: the reply
    goes to the customer and the teammate is copied. When no customer can be
    found, `to` is "" (send_reply refuses) rather than the teammate.
    """
    threads = _threads(ticket_id)
    latest = _newest(threads, "in")
    sender = _sender(latest)
    to = sender
    author = (latest.get("author") or {}).get("name") or ""
    given = (latest.get("author") or {}).get("firstName") or ""
    extra = emails_in(latest.get("cc"))
    forwarded_by = ""
    if sender and is_team_address(sender):
        forwarded_by = sender
        to, author, extra = _customer_for(ticket_id, threads, latest)
        given = ""
    cc: list[str] = []
    for address in (extra + _secondary_emails(ticket_id)
                    + team_cc(threads)):
        if address != to and not _ours(address) and address not in cc:
            cc.append(address)
    return {
        "to": to,
        "cc": cc,
        "first_name": first_name(author, to, given) if to else "",
        "name": author,
        "forwarded_by": forwarded_by,
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

    Tagged `agent-fix`, with a private note linking the PR, and moved to
    the Agent Fixes team at In Progress (`set_agent_fixing`). Sam,
    2026-10-24: Processing means a person owes the client a reply, and
    once the fix is open the fix email is that reply, so the ticket leaves
    his list. The one exception is a ticket we have never answered: it
    stays New until Sam's acknowledgment goes out (`sprint_thread.
    _after_send`), or the client hears nothing until the fix ships. A
    client writing again flips it back to Processing (Zoho's reply
    workflow) and gets a fresh card (shadow_triage). Each part fails on
    its own and is reported, never raised: the PR already exists.
    """

    def move():
        if latest_outbound(ticket_id):
            set_agent_fixing(ticket_id)

    notes = []
    steps = (
        ("status", move),
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
    """The agent's fix is in flight: In Progress, owned by the Agent Fixes
    team with no agent, so it leaves Sam's Support Agent list. The client
    sees only In Progress."""
    return _send("PATCH", f"/tickets/{ticket_id}",
                 {"status": "In Progress", "teamId": AGENT_FIXES_TEAM,
                  "assigneeId": None})


def unmark_agent_fix(ticket_id: str, pr_url: str) -> str:
    """The fix PR was closed unmerged: nobody is fixing this any more.

    Back to Processing (needs attention), off the Agent Fixes team and
    back on Sam, with the tag removed and a note saying why, so Zoho does
    not keep claiming the agent has it.
    """
    notes = []
    steps = (
        ("status", lambda: _send("PATCH", f"/tickets/{ticket_id}",
                                 {"status": "Processing",
                                  "assigneeId": SAM_AGENT})),
        ("team", lambda: _send("PATCH", f"/tickets/{ticket_id}",
                               {"teamId": None})),
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
