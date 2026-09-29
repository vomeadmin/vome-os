"""
sprint.py

Run a support sprint from the terminal.

    py sprint.py                    triage the open queue, print the cards
    py sprint.py --limit 10         first 10 only
    py sprint.py --ticket 9101      one ticket, for debugging
    py sprint.py --json out.json    also write the cards to a file

WHAT THIS IS FOR
----------------
This is how the triage director gets tested before it is trusted, and how a
sprint is run while that is still true. It pulls the real queue, gathers the
context a reviewer would otherwise gather by hand, runs the agent, and prints
the cards.

**It sends nothing and changes nothing.** No Zoho status, no ClickUp task, no
Slack message, no email. It is a read-and-think pass whose entire output is
text in a terminal. That is deliberate: the agent has to earn a reviewer's
trust on cards before anything downstream of a card is automated.

WHERE THIS LIVES AND WHY
------------------------
Repository root, not `vomeos/`. It reaches Zoho, ClickUp and the CRM, which
are the support application's integrations, and the kernel may not import an
application. The kernel's contribution is one line: `run_agent(...)`.

THE THREE STAGES
----------------
1. **Now.** This script, on your machine, on demand. You read every card.
2. **Shadow.** The same agent on the worker queue, triggered by the existing
   Zoho webhook, writing its card to the ticket as an internal note while you
   keep running sprints by hand. You compare its card to your own call.
3. **Live.** The card is waiting before you sit down, because the work
   happened when the ticket arrived rather than when you opened your laptop.

Stage 3 is the answer to "what happens when I am not online": the agent does
not need you to be. It needs the worker running.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from agent import (  # noqa: E402
    _extract_ticket_fields,
    _format_conversations,
    fetch_crm_account,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
    _unwrap_mcp_result,
    _zoho_desk_call,
)
from vomeos import run_agent  # noqa: E402

AGENT = "customer_success.triage_director"

# Statuses that make up the sprint working set.
OPEN_STATUSES = ("New", "Processing")

IMPORTANT_ARR = 1500

# Zoho pages at 100. Three pages covers a queue several times the size of the
# current one, and stops early when a short page says there is no more.
PAGE_SIZE = 100
MAX_PAGES = 3


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

class QueueUnavailable(RuntimeError):
    """The queue could not be read. Not the same as the queue being empty."""


def fetch_open_tickets(limit: int = 25) -> list[dict]:
    """The sprint working set, oldest activity first.

    Two Zoho quirks are handled here rather than trusted to the API. The
    `status` parameter rejects "New" and "Processing" outright, and the
    `filters` form is accepted and then silently ignored, so filtering
    happens on what comes back.

    A failed call raises rather than returning an empty list. Reporting "the
    queue is clear" when the truth is "we could not read the queue" is the
    same false-reassurance failure this whole division exists to prevent, and
    it would be worse here because it looks like good news.
    """
    collected: list[dict] = []
    for page in range(MAX_PAGES):
        rows = _pull_page(offset=page * PAGE_SIZE)
        collected.extend(rows)
        if len(rows) < PAGE_SIZE:
            break

    open_rows = [r for r in collected if r.get("status") in OPEN_STATUSES]

    # The playbook orders a sprint oldest first, measured by the most recent
    # response on the thread: the ticket that has been waiting longest since
    # anything happened on it. Zoho can only sort newest-first across the
    # whole queue (ascending returns nothing but long-closed tickets), so the
    # ordering is restored here over the open set.
    open_rows.sort(key=_last_activity)
    return open_rows[:limit]


def _pull_page(offset: int) -> list[dict]:
    """One page of tickets, newest activity first.

    `-recentThread` rather than `recentThread`: ascending returns the oldest
    tickets in the whole system, which are all long closed, and the open
    queue never appears at all.
    """
    result = _zoho_desk_call(
        "ZohoDesk_getTickets",
        {
            "query_params": {
                "limit": PAGE_SIZE,
                "from": offset,
                "sortBy": "-recentThread",
            }
        },
    )
    if not result:
        from agent import get_last_mcp_error

        raise QueueUnavailable(get_last_mcp_error() or "Zoho returned nothing")

    # The MCP envelope wraps the payload, and Zoho nests `data` inside
    # `data`. Unwrap both rather than assuming either shape.
    payload = _unwrap_mcp_result(result)
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        raise QueueUnavailable(
            f"unexpected response shape: {type(data).__name__}"
        )
    return data


def _last_activity(row: dict) -> str:
    """When anything last happened on this ticket. Sorts as an ISO string."""
    return str(
        row.get("customerResponseTime")
        or row.get("modifiedTime")
        or row.get("createdTime")
        or ""
    )


def find_existing_tasks(subject: str, fields: dict) -> str:
    """What the reviewer would search for before proposing new work.

    Delegates to clickup_search, which is explicit about the difference
    between "nothing found" and "we did not look".
    """
    from clickup_search import describe_matches

    query = f"{subject} {fields.get('description', '')}"
    return describe_matches(query)


def find_kb_matches(subject: str, body: str) -> str:
    """Product knowledge that may already answer this.

    Without it the agent cannot tell "works as designed" from "does not
    exist", so it correctly refuses to call anything user_education. That
    showed up immediately in the answer key: two cases came back needs_human
    purely for want of a knowledge source.

    Not the source code. See product_knowledge.py for why.
    """
    from product_knowledge import describe_knowledge

    query = f"{subject} {body}".strip()
    if not query:
        return "No product knowledge search was run (empty ticket)."
    return describe_knowledge(query)


def build_context(ticket_row: dict) -> dict | None:
    """Everything the agent needs about one ticket."""
    ticket_id = ticket_row.get("id")
    raw = fetch_ticket_from_zoho(ticket_id)
    if not raw:
        return None
    fields = _extract_ticket_fields(raw)
    conversations = fetch_ticket_conversations(ticket_id)
    thread = _format_conversations(conversations)

    account_name = ""
    arr = 0
    try:
        crm = fetch_crm_account(fields.get("contact_email", "")) or {}
        account_name = crm.get("account_name") or ""
        arr = crm.get("arr") or 0
    except Exception:
        pass

    tier = "unknown tier"
    try:
        from agent import get_client_tier

        tier = get_client_tier(arr)
    except Exception:
        pass

    account = (
        f"{account_name or ticket_row.get('email', 'unknown account')} "
        f"({tier}, {arr} CAD combined ARR)"
    )

    attachments = fields.get("attachments") or "none"
    if isinstance(attachments, (list, tuple)):
        attachments = ", ".join(str(a) for a in attachments) or "none"

    return {
        "ticket_number": str(ticket_row.get("ticketNumber") or ticket_id),
        "account": account,
        "thread": thread or "(no readable conversation)",
        "attachments": attachments,
        "existing_tasks": find_existing_tasks(
            fields.get("subject", ""), fields
        ),
        "kb_matches": find_kb_matches(
            fields.get("subject", ""), fields.get("description", "")
        ),
        "_ticket_id": ticket_id,
        "_subject": fields.get("subject", ""),
        "_arr": arr,
    }


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def render_card(number: str, subject: str, account: str, card: dict) -> str:
    important = (
        "IMPORTANT CLIENT" if card.get("important_client") else "standard"
    )
    confidence = str(card.get("confidence", "?")).upper()
    lines = [
        "",
        "=" * 74,
        f"#{number}  {account}  [{important}]",
        f"  {subject[:70]}",
        "-" * 74,
        f"CATEGORY      {card.get('category', '?')}  (confidence "
        f"{confidence})",
    ]

    def block(label: str, key: str) -> None:
        value = str(card.get(key) or "").strip()
        if value:
            wrapped = textwrap.fill(
                value, width=70, initial_indent="", subsequent_indent=" " * 14
            )
            lines.append(f"{label.ljust(13)} {wrapped}")

    block("LIVE Q", "live_question")
    block("SITUATION", "situation")
    block("EXISTING", "existing_work")
    block("RECIPIENTS", "recipients")
    block("MISSING", "missing_information")
    block("ACTION", "recommended_action")

    claims = str(card.get("blocking_claims") or "").strip()
    if claims:
        lines.append("")
        lines.append("  !! UNBACKED CLAIM ALREADY IN THIS THREAD:")
        lines.append(
            textwrap.fill(claims, width=70, initial_indent="     ",
                          subsequent_indent="     ")
        )

    why = str(card.get("why_human") or "").strip()
    if why:
        lines.append("")
        lines.append("  >> NEEDS A PERSON:")
        lines.append(
            textwrap.fill(why, width=70, initial_indent="     ",
                          subsequent_indent="     ")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(limit: int, ticket: str = "", out_path: str = "") -> int:
    if ticket:
        rows = [{"id": ticket, "ticketNumber": ticket}]
    else:
        print("Pulling the queue...")
        try:
            rows = fetch_open_tickets(limit)
        except QueueUnavailable as exc:
            print(f"\nCould not read the queue: {exc}")
            print(
                "This is NOT an empty queue. Nothing was triaged. Check the "
                "Zoho credentials in .env and try again."
            )
            return 1
        print(f"{len(rows)} open ticket(s) in the working set.\n")

    if not rows:
        print(
            "No tickets in New or Processing. The queue really is clear "
            "(the pull succeeded)."
        )
        return 0

    cards = []
    escalations = 0
    claims_found = 0

    for index, row in enumerate(rows, start=1):
        number = str(row.get("ticketNumber") or row.get("id"))
        print(f"[{index}/{len(rows)}] triaging #{number}...", flush=True)
        context = build_context(row)
        if not context:
            print(f"  could not load ticket #{number}, skipping")
            continue

        subject = context.pop("_subject", "")
        ticket_id = context.pop("_ticket_id", "")
        context.pop("_arr", None)

        result = run_agent(
            AGENT,
            context=context,
            subject_type="zoho_ticket",
            subject_id=str(ticket_id),
        )
        if not result.ok:
            print(f"  no card ({result.why()})")
            continue

        card = result.data or {}
        print(render_card(number, subject, context["account"], card))
        cards.append({"ticket": number, "subject": subject, **card})
        if card.get("category") == "needs_human" or card.get(
            "confidence"
        ) == "low":
            escalations += 1
        if str(card.get("blocking_claims") or "").strip():
            claims_found += 1

    print("\n" + "=" * 74)
    print(f"{len(cards)} card(s). {escalations} need a person. "
          f"{claims_found} thread(s) carry an unbacked claim.")
    print("Nothing was sent and nothing was changed.")

    if out_path:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "agent": AGENT,
            "cards": cards,
        }
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"Wrote {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--ticket", default="")
    parser.add_argument("--json", dest="out_path", default="")
    args = parser.parse_args()
    return run(args.limit, args.ticket, args.out_path)


if __name__ == "__main__":
    raise SystemExit(main())
