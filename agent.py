import json
import os
import re
import sys

import anthropic
import httpx
from pathlib import Path

from clickup_tasks import close_clickup_task
from status_constants import (
    normalize_status,
    CU_WRITE_QUEUED_UPPER,
    THREAD_OPEN,
    THREAD_HANDLED,
    THREAD_CLOSED,
    THREAD_ON_PROD_SENT,
    THREAD_WAITING_CLIENT,
    CU_AWAITING_CLIENT,
    CU_WAITING_ON_CLIENT,
    CU_ON_PROD,
    CU_QUEUED,
    CU_IN_PROGRESS,
    CU_ON_DEV,
    CU_DONE,
    CU_ESCALATED,
    CU_NEEDS_REVIEW,
    ZOHO_PROCESSING,
    ZOHO_IN_PROGRESS,
    ZOHO_ON_HOLD,
    ZOHO_CLOSED,
    ZOHO_RESOLVED,
    ZOHO_AWAITING_CLIENT_RESPONSE,
)
from error_reports import is_error_report
from model_config import SUPPORT_MODEL, SUPPORT_MODEL_FAST

# Fix Windows console encoding for emoji in system_prompt.md
try:
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

client = anthropic.Anthropic()

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

# Load response templates so Claude can reference them when drafting replies
_templates_path = Path(__file__).parent / "response_templates.md"
if _templates_path.exists():
    _templates_content = _templates_path.read_text(encoding="utf-8")
    SYSTEM_PROMPT += (
        "\n\n---\n\n"
        "## RESPONSE TEMPLATES\n\n"
        "The following are proven response templates for common support "
        "scenarios. When drafting a reply, check if a template fits the "
        "situation. If one does, use it as the base and personalize with "
        "the client's name and specific details from the ticket. You may "
        "adapt the wording to fit context, but preserve key links and "
        "instructions from the template. If Sam references a template by "
        "name (e.g. 'use the ForgotPassword template'), use that template "
        "as the foundation for the draft.\n\n"
        f"{_templates_content}"
    )

# Load the feature catalog (generated from landing-page strings) so Claude
# can ground answers about what Vome offers, plan limits, and pricing in
# the same copy customers see on the marketing site.
_catalog_path = Path(__file__).parent / "knowledge_book" / "feature_catalog.md"
if _catalog_path.exists():
    _catalog_content = _catalog_path.read_text(encoding="utf-8")
    SYSTEM_PROMPT += (
        "\n\n---\n\n"
        "## FEATURE CATALOG (FROM LANDING PAGE)\n\n"
        "The following catalog is the authoritative reference for what "
        "features Vome ships, how each module is described publicly, and "
        "what each plan tier (Recruit / Pro / Enterprise / Ultimate) "
        "includes. Use it when drafting replies to questions about "
        "capabilities, pricing, plan limits, integrations, or how a "
        "particular feature works. If a question describes something this "
        "catalog covers, ground the answer in this copy rather than "
        "guessing. If a request describes something the catalog does NOT "
        "cover, treat it as a potential feature request and flag it for "
        "human review.\n\n"
        f"{_catalog_content}"
    )

ZOHO_DESK_MCP_URL = os.environ.get("ZOHO_DESK_MCP_URL", "")
ZOHO_CRM_MCP_URL = os.environ.get("ZOHO_CRM_MCP_URL", "")
ZOHO_ORG_ID = os.environ.get("ZOHO_ORG_ID", "")

NOTE_HEADER = "\U0001f916 AGENT ANALYSIS \u2014 DO NOT SEND \u2014 FOR REVIEW ONLY"
UPDATE_HEADER = "\U0001f504 AGENT UPDATE \u2014 CLIENT REPLIED"
TEAM_EMAILS = {
    "admin@vomevolunteer.com",
    "sam@vomevolunteer.com",
    "s.fagen@vomevolunteer.com",
    "r.segev@vomevolunteer.com",
}
ZOHO_FROM_ADDRESS = os.environ.get("ZOHO_FROM_ADDRESS", "admin@vomevolunteer.com")


def _mcp_post(url: str, tool_name: str, arguments: dict) -> dict | None:
    """Send a tools/call JSON-RPC request to an MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    try:
        resp = httpx.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            body_preview = resp.text[:500]
            print(
                f"MCP HTTP {resp.status_code}"
                f" ({tool_name}): {body_preview}"
            )
            _last_mcp_error[0] = (
                f"HTTP {resp.status_code}: {body_preview}"
            )
            return None
        result = resp.json()
        if "error" in result:
            print(
                f"MCP error ({tool_name}): {result['error']}"
            )
            _last_mcp_error[0] = str(result["error"])
            return None
        # Check for isError in the result content
        res = result.get("result", result)
        if isinstance(res, dict) and res.get("isError"):
            content = res.get("content", [])
            err_text = ""
            for block in content:
                if block.get("type") == "text":
                    err_text = block.get("text", "")
                    break
            print(
                f"MCP tool error ({tool_name}): {err_text}"
            )
            _last_mcp_error[0] = err_text or str(res)
            return res  # Return so caller can inspect
        _last_mcp_error[0] = ""
        return res
    except Exception as e:
        print(f"MCP request failed ({tool_name}): {e}")
        _last_mcp_error[0] = str(e)
        return None


# Stores the last MCP error message for surfacing in Slack
_last_mcp_error: list[str] = [""]


def get_last_mcp_error() -> str:
    """Return the last MCP error message (for Slack display)."""
    return _last_mcp_error[0]


def _zoho_desk_call(tool_name: str, arguments: dict) -> dict | None:
    """Call a ZohoDesk tool via the Desk MCP server."""
    if not ZOHO_DESK_MCP_URL:
        print(f"ZOHO_DESK_MCP_URL not set -- skipping {tool_name}")
        return None
    return _mcp_post(ZOHO_DESK_MCP_URL, tool_name, arguments)


def _zoho_crm_call(tool_name: str, arguments: dict) -> dict | None:
    """Call a ZohoCRM tool via the CRM MCP server."""
    if not ZOHO_CRM_MCP_URL:
        print(f"ZOHO_CRM_MCP_URL not set -- skipping {tool_name}")
        return None
    return _mcp_post(ZOHO_CRM_MCP_URL, tool_name, arguments)


def _unwrap_mcp_result(result: dict | list | None) -> dict | list | None:
    """Unwrap MCP content wrapper to get the raw JSON data."""
    if result is None:
        return None
    if isinstance(result, dict) and "content" in result:
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, TypeError):
                    pass
    return result


def fetch_ticket_from_zoho(ticket_id: str) -> dict | None:
    """Fetch full ticket details from Zoho Desk including contact info."""
    result = _zoho_desk_call("ZohoDesk_getTicket", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "include": "contacts,assignee",
        },
    })
    if result:
        print(f"Fetched ticket {ticket_id} from Zoho Desk")
    else:
        print(f"Failed to fetch ticket {ticket_id} from Zoho Desk")
    return result


# Cap on per-thread body fetches during hydration. The conversations list is
# itself capped at 100 entries, so this is a latency backstop for pathological
# threads rather than a limit we expect to hit in practice.
_MAX_THREAD_HYDRATIONS = 50


def _hydrate_conversation_threads(
    ticket_id: str, result: dict | list | None
) -> dict | list | None:
    """Fill in the full body of each email thread entry in a conversations
    result.

    ``getTicketConversations`` is a LIST endpoint: for thread (email) entries it
    returns only a short ``summary`` snippet, not the full ``content``. Rendering
    those with the summary fallback means every draft sees a one-line preview of
    the client's actual messages instead of the real text. Here we fetch the full
    body per thread via ``getThread`` (which carries ``content``/``plainText``).

    Comments already carry full ``content`` and are left untouched. The return
    value is re-wrapped into the same MCP text-block shape as the input so every
    existing consumer keeps working; on any problem the original result is
    returned unchanged.
    """
    data = _unwrap_mcp_result(result)
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        return result

    fetched = 0
    hydrated_any = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Comments already include their full body; only email threads are
        # summary-only in the list response.
        if entry.get("type") == "comment":
            continue
        if (entry.get("content") or "").strip():
            continue
        thread_id = str(entry.get("id") or "").strip()
        if not thread_id:
            continue
        if fetched >= _MAX_THREAD_HYDRATIONS:
            print(
                f"[CONVERSATIONS] Thread hydration capped at "
                f"{_MAX_THREAD_HYDRATIONS} for ticket {ticket_id}; remaining "
                "messages left as summaries"
            )
            break

        thread_result = _zoho_desk_call("ZohoDesk_getThread", {
            "path_variables": {
                "ticketId": str(ticket_id),
                "threadId": thread_id,
            },
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "include": "plainText",
            },
        })
        fetched += 1
        thread = _unwrap_mcp_result(thread_result)
        if not isinstance(thread, dict):
            continue
        full = (thread.get("plainText") or thread.get("content") or "").strip()
        if full:
            entry["content"] = full
            hydrated_any = True

    if not hydrated_any:
        return result

    # Re-wrap into the MCP text-block shape so downstream _unwrap_mcp_result
    # re-parses the hydrated payload rather than the original summaries.
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


def fetch_ticket_conversations(ticket_id: str) -> dict | None:
    """Fetch all conversation threads and comments for a ticket.

    Thread (email) bodies are hydrated from ``summary`` snippets to their full
    text so downstream drafting/analysis sees the real client messages. See
    ``_hydrate_conversation_threads``.
    """
    result = _zoho_desk_call("ZohoDesk_getTicketConversations", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "from": 0,
            "limit": 100,
        },
    })
    if result:
        result = _hydrate_conversation_threads(ticket_id, result)
        print(f"Fetched conversations for ticket {ticket_id}")
    else:
        print(f"Failed to fetch conversations for ticket {ticket_id}")
    return result


def _extract_ticket_fields(raw_result: dict) -> dict:
    """Extract relevant fields from a Zoho ticket API response."""
    ticket = _unwrap_mcp_result(raw_result) or {}
    if not isinstance(ticket, dict):
        ticket = {}

    # Contact info from the included contacts data
    contact = ticket.get("contact", {}) or {}
    first = contact.get("firstName", "") or ""
    last = contact.get("lastName", "") or ""
    contact_name = f"{first} {last}".strip()
    if not contact_name:
        contact_name = (
            contact.get("name", "")
            or ticket.get("email", "")
            or "unknown"
        )
    contact_email = (
        contact.get("email", "")
        or ticket.get("email", "")
        or ""
    )

    cc_email = _ticket_cc_emails(ticket, exclude=contact_email)
    # A teammate the client wrote to (Ron, #9610) stays on every reply.
    # _ticket_cc_emails drops all Vome addresses, so add them back here.
    if ticket.get("id"):
        import zoho_actions
        cc = [e for e in cc_email.split(",") if e]
        for email in zoho_actions.ticket_teammates(str(ticket["id"])):
            if email.lower() not in {c.lower() for c in cc}:
                cc.append(email)
        cc_email = ",".join(cc)

    # Origin signals — distinguish a client-submitted ticket (inbound email /
    # web form) from one an agent created by hand inside Zoho Desk. Zoho marks
    # agent/UI/API-created tickets with source.type == "SYSTEM".
    source = ticket.get("source", {}) or {}
    source_type = (source.get("type") or "").strip()

    return {
        "subject": ticket.get("subject", ""),
        "description": ticket.get("description", ""),
        "contact_name": contact_name,
        "contact_email": contact_email,
        "cc_email": cc_email,
        "status": ticket.get("status", ""),
        "ticket_number": str(ticket.get("ticketNumber", "") or ""),
        "created_time": ticket.get("createdTime", ""),
        "channel": ticket.get("channel", "") or "",
        "source_type": source_type,
    }


def _is_own_address(email: str) -> bool:
    """True for addresses that are Vome's own (team inboxes, the Zoho Desk
    support alias). Copying those on a client reply only loops mail back."""
    email = email.strip().lower()
    return (
        email in TEAM_EMAILS
        or email == ZOHO_FROM_ADDRESS.lower()
        or email.endswith(("@vomevolunteer.com", "@vomevolunteer.co",
                           "@vomevolunteer.zohodesk.com"))
    )


def _fetch_contact_email(contact_id: str) -> str:
    """Resolve a Zoho Desk contact id to its email, or "" on any failure."""
    result = _zoho_desk_call("ZohoDesk_getContact", {
        "path_variables": {"contactId": str(contact_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    data = _unwrap_mcp_result(result) if result else None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        print(f"[CC] Could not resolve secondary contact {contact_id}")
        return ""
    return (data.get("email") or "").strip()


def _ticket_cc_emails(ticket: dict, exclude: str = "") -> str:
    """Comma-separated CC list for a client reply on this ticket.

    The address a client types into the portal's CC box is NOT on the ticket
    as a ``cc`` field (getTicket has no such key). Zoho stores it as a
    secondary contact, a list of contact ids under ``secondaryContacts``.
    Replies sent from the Zoho UI copy secondary contacts automatically;
    ``sendReply`` over the API does not, so every Vic reply silently dropped
    them (ticket #9313, Kimberly Crest). Resolve each id to its email here.

    The legacy ``cc`` key is still honoured in case Zoho ever returns it.
    Vome's own addresses and the primary contact are left out.
    """
    candidates: list[str] = []
    raw_cc = ticket.get("cc") or ""
    if isinstance(raw_cc, list):
        candidates.extend(str(c) for c in raw_cc)
    else:
        candidates.extend(str(raw_cc).split(","))
    for contact_id in ticket.get("secondaryContacts") or []:
        if contact_id:
            candidates.append(_fetch_contact_email(str(contact_id)))

    seen = {(exclude or "").strip().lower()}
    emails: list[str] = []
    for raw in candidates:
        # Strip a display-name wrapper: '"Janet" <janet@x.com>' -> janet@x.com
        match = re.search(r"[^\s<>\"',]+@[^\s<>\"',]+", raw or "")
        if not match:
            continue
        email = match.group(0)
        key = email.lower()
        if key in seen or _is_own_address(email):
            continue
        seen.add(key)
        emails.append(email)
    return ",".join(emails)


def fetch_ticket_cc(ticket_id: str) -> str:
    """CC list for a client reply, for send paths that do not already hold
    the ticket fields. Returns "" when the ticket cannot be fetched, so a
    lookup failure never blocks the reply itself."""
    raw = fetch_ticket_from_zoho(ticket_id)
    if not raw:
        return ""
    return _extract_ticket_fields(raw).get("cc_email", "")


def _format_conversations(raw_result: dict | list | None) -> str:
    """Format conversation threads into a readable chronological string."""
    if not raw_result:
        return "(No conversation threads found)"

    data = _unwrap_mcp_result(raw_result)

    # Normalize to a list of conversation entries
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list) or len(data) == 0:
        return "(No conversation threads found)"

    lines = []
    for entry in data:
        author = entry.get("author", {}) or {}
        author_name = author.get("name", "Unknown")
        timestamp = entry.get("createdTime", entry.get("sendDateTime", ""))
        content = entry.get("content", entry.get("summary", ""))
        is_public = entry.get("isPublic", True)
        visibility = "" if is_public else " [INTERNAL]"
        direction = entry.get("direction", "")

        header = f"--- {author_name}"
        if direction:
            header += f" [{direction}]"
        header += f"{visibility} -- {timestamp} ---"

        lines.append(header)
        lines.append(content if content else "(no content)")
        lines.append("")

    return "\n".join(lines)


def _normalize_tier(offering: str | None) -> str:
    """Map the CRM Offering field to a clean tier name."""
    if not offering:
        return "Unknown"
    normalized = offering.strip().lower()
    for tier in ("ultimate", "enterprise", "pro", "recruit"):
        if tier in normalized:
            return tier.capitalize()
    return "Unknown"


def _is_auth_error(result: dict | None) -> bool:
    """Check if an MCP result contains a 401/authorization error."""
    if result is None:
        return False
    raw = json.dumps(result, default=str).lower()
    return "401" in raw or "unauthorized" in raw or "invalid_token" in raw


def _fetch_desk_fallback(contact_email: str) -> dict:
    """Fall back to Zoho Desk contact/account search when CRM is unavailable."""
    not_found = {"found": False, "contact_type": "volunteer", "enrichment_source": "Desk only — CRM unavailable"}

    try:
        result = _zoho_desk_call("ZohoDesk_searchContacts", {
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "_all": contact_email,
                "limit": "1",
            },
        })
        data = _unwrap_mcp_result(result)

        if not data:
            print(f"Desk fallback: contact not found ({contact_email})")
            return not_found

        # Normalize to list
        contacts = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(contacts, list) or not contacts:
            print(f"Desk fallback: contact not found ({contact_email})")
            return not_found

        contact = contacts[0]
        contact_id = str(contact.get("id", ""))
        contact_name = contact.get("name") or contact.get("lastName") or "unknown"
        account_id = contact.get("accountId") or contact.get("account", {}).get("id") if isinstance(contact.get("account"), dict) else contact.get("accountId")
        print(f"Desk fallback: found contact {contact_name} ({contact_id})")

        enrichment = {
            "found": True,
            "contact_type": "admin",
            "account_name": None,
            "tier": "Unknown",
            "arr": None,
            "currency": None,
            "account_id": str(account_id) if account_id else None,
            "contact_id": contact_id,
            "enrichment_source": "Desk only — CRM unavailable",
        }

        if not account_id:
            print("Desk fallback: no account ID on contact")
            return enrichment

        acct_result = _zoho_desk_call("ZohoDesk_getAccount", {
            "path_variables": {"accountId": str(account_id)},
            "query_params": {"orgId": str(ZOHO_ORG_ID)},
        })
        acct_data = _unwrap_mcp_result(acct_result)

        if acct_data and isinstance(acct_data, dict):
            enrichment["account_name"] = acct_data.get("accountName") or acct_data.get("name")
            print(f"Desk fallback: account {enrichment['account_name']} ({account_id})")
        else:
            print("Desk fallback: could not fetch account details")

        return enrichment

    except Exception as e:
        print(f"Desk fallback failed: {e}")
        return not_found


SOCIAL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "me.com", "msn.com", "mail.com", "ymail.com",
}


def _crm_search_contacts(query_params: dict) -> list | None:
    """Search CRM contacts. Returns list of contacts or None."""
    # Limit fields to prevent response truncation from MCP server
    params = {
        **query_params,
        "fields": "Full_Name,Email,Account_Name,FV_Offering,Offering",
    }
    result = _zoho_crm_call("ZohoCRM_searchRecords", {
        "path_variables": {"module": "Contacts"},
        "query_params": params,
    })
    if _is_auth_error(result):
        return None
    data = _unwrap_mcp_result(result)
    if not data or not isinstance(data, dict):
        return []
    return data.get("data", [])


def fetch_crm_account(
    contact_email: str,
    contact_name: str = "",
) -> dict:
    """Look up a contact in Zoho CRM by email, domain, or name."""
    not_found = {"found": False, "contact_type": "volunteer"}

    if not contact_email:
        print("CRM step 1: no email provided — skipping")
        return not_found

    try:
        # Step 1a — exact email match
        contacts = _crm_search_contacts({"email": contact_email})
        if contacts is None:
            print("CRM auth error — falling back to Desk")
            return _fetch_desk_fallback(contact_email)

        if contacts:
            print(f"CRM step 1a: exact email match ({contact_email})")
        else:
            print(f"CRM step 1a: no exact match ({contact_email})")

        # Step 1b — domain search (skip social email providers)
        if not contacts:
            domain = contact_email.split("@")[-1].lower()
            if domain not in SOCIAL_DOMAINS:
                print(f"CRM step 1b: trying domain {domain}")
                domain_contacts = _crm_search_contacts(
                    {"email": domain}
                )
                if domain_contacts:
                    contacts = domain_contacts
                    print(
                        f"CRM step 1b: domain match — "
                        f"found {len(contacts)} contact(s)"
                    )
                else:
                    print(f"CRM step 1b: no domain match")
            else:
                print(f"CRM step 1b: social domain, skipping")

        # Step 1c — name search as last resort
        if not contacts and contact_name:
            name_parts = contact_name.strip().split()
            if len(name_parts) >= 2:
                last = name_parts[-1]
                print(f"CRM step 1c: trying name '{last}'")
                name_contacts = _crm_search_contacts(
                    {"word": last}
                )
                if name_contacts:
                    contacts = name_contacts
                    print(
                        f"CRM step 1c: name match — "
                        f"found {len(contacts)} contact(s)"
                    )
                else:
                    print(f"CRM step 1c: no name match")

        if not contacts:
            print(f"CRM: all lookups exhausted — not found")
            return not_found

        contact = contacts[0]
        contact_name = contact.get("Full_Name", "unknown")
        contact_id = str(contact.get("id", ""))
        print(f"CRM step 1: found contact {contact_name} ({contact_id})")

        account_info = contact.get("Account_Name") or {}
        account_name = account_info.get("name")
        account_id = str(account_info.get("id", "")) if account_info.get("id") else None

        # Use the highest tier across all contacts at this account
        tier_rank = {
            "ultimate": 4, "enterprise": 3, "pro": 2, "recruit": 1,
        }
        best_tier = "Unknown"
        best_rank = -1
        for c in contacts:
            c_offering = c.get("FV_Offering") or c.get("Offering")
            if isinstance(c_offering, list):
                c_offering = c_offering[0] if c_offering else None
            c_tier = _normalize_tier(c_offering)
            rank = tier_rank.get(c_tier.lower(), 0)
            if rank > best_rank:
                best_rank = rank
                best_tier = c_tier
        tier = best_tier
        if len(contacts) > 1:
            print(f"CRM: {len(contacts)} contacts found — using highest tier: {tier}")

        enrichment = {
            "found": True,
            "contact_type": "admin",
            "account_name": account_name,
            "tier": tier,
            "arr": None,
            "currency": None,
            "account_id": account_id,
            "contact_id": contact_id,
        }

        # Step 2 — get related Deals for the account
        if not account_id:
            print("CRM step 2: no account ID — skipping deal lookup")
            return enrichment

        deal_result = _zoho_crm_call("ZohoCRM_searchRecords", {
            "path_variables": {"module": "Deals"},
            "query_params": {
                "criteria": f"(Account_Name:equals:{account_id})",
                "fields": "Deal_Name,Stage,Amount,Currency",
            },
        })

        if _is_auth_error(deal_result):
            print("CRM authorization error on deals — returning contact-only enrichment")
            return enrichment

        deal_data = _unwrap_mcp_result(deal_result)

        if not deal_data or not isinstance(deal_data, dict):
            print("CRM step 2: no deals found")
            return enrichment

        deals = deal_data.get("data", [])
        if not deals:
            print("CRM step 2: no deals found")
            return enrichment

        # Prefer first Closed Won deal, fall back to any deal
        chosen = None
        for deal in deals:
            stage = (deal.get("Stage") or "").lower()
            if "closed won" in stage:
                chosen = deal
                break
        if not chosen:
            chosen = deals[0]

        amount = chosen.get("Amount")
        if amount is not None:
            enrichment["arr"] = str(amount)
            enrichment["currency"] = chosen.get("Currency") or None
            print(f"CRM step 2: deal found — {enrichment['arr']} {enrichment['currency']}")
        else:
            print("CRM step 2: deal found but no Amount")

        return enrichment

    except Exception as e:
        print(f"CRM lookup failed: {e}")
        print("Attempting Desk fallback...")
        return _fetch_desk_fallback(contact_email)


def _detect_language(text: str) -> str | None:
    """Detect non-English content by checking for common French patterns.

    Returns the language name if non-English detected, else None.
    """
    if not text:
        return None
    lower = text.lower()
    # Common French words/patterns unlikely in English
    french_markers = [
        " je ", " nous ", " vous ", " les ", " des ", " une ", " est ",
        " sont ", " dans ", " pour ", " avec ", " sur ", " pas ",
        " mais ", " aussi ", " cette ", " notre ", " votre ",
        "bonjour", "merci", "s'il vous", "bénévol",
    ]
    hits = sum(1 for m in french_markers if m in f" {lower} ")
    if hits >= 3:
        return "French"
    return None


def get_client_tier(arr) -> str:
    """Derive client tier from CRM ARR value.

    Returns one of: very-high, high, medium, low.
    """
    if arr is None:
        return "low"
    try:
        value = float(arr)
    except (ValueError, TypeError):
        return "low"
    if value >= 4000:
        return "very-high"
    if value >= 1500:
        return "high"
    if value >= 1000:
        return "medium"
    return "low"


# -- Zoho agent IDs for routing --
ZOHO_AGENT_SANJAY = "569440000023159001"
ZOHO_AGENT_ONLYG = "569440000023160001"
ZOHO_AGENT_SAM = "569440000000139001"


# ---------------------------------------------------------------------------
# Auth bypass detection (email channel)
# ---------------------------------------------------------------------------

# A forwarded ticket passes on someone else's message, so it is never the
# sender's own sign-in problem.
_FORWARDED_SUBJECT_RE = re.compile(r"^\s*(?:re\s*:\s*)*(?:fw|fwd|tr)\s*:", re.IGNORECASE)

AUTH_TICKET_SELF = "self"
AUTH_TICKET_OTHER = "other"


def _classify_auth_ticket(subject: str, body: str) -> str | None:
    """Who has the sign-in problem: the sender, someone else, or nobody.

    AUTH_TICKET_SELF: the sender cannot get into their own account; the auth
    check and activation run on their email.
    AUTH_TICKET_OTHER: the sender (usually an admin) writes about a volunteer
    who cannot sign in or never got the invitation. Checking the sender's
    account answers the wrong question (#9549).
    None: not a sign-in problem.
    """
    text = f"Subject: {subject}\n\nBody: {body or ''}".strip()
    if not text:
        return None
    try:
        response = client.messages.create(
            model=SUPPORT_MODEL_FAST,
            max_tokens=5,
            system=(
                "You read a support ticket and decide who has a sign-in "
                "problem. Sign-in problems include: not receiving a "
                "verification, activation or invitation email, can't "
                "authenticate or verify an email address, account not "
                "yet activated, or similar sign-up/login blockers.\n"
                "Reply SELF when the person who wrote the ticket has the "
                "problem with their own account.\n"
                "Reply OTHER when they write about someone else who has "
                "it, for example an admin or coordinator reporting that a "
                "volunteer can't log in or never received their invitation, "
                "or a forwarded message from that person.\n"
                "Reply NO for anything else.\n"
                "Reply with only SELF, OTHER or NO."
            ),
            messages=[{"role": "user", "content": text}],
        )
        answer = response.content[0].text.strip().upper()
    except Exception as e:
        print(f"[AUTH BYPASS] Detection failed: {e}")
        return None
    if answer.startswith("OTHER"):
        print("[AUTH BYPASS] Claude detected a sign-in problem for someone else")
        return AUTH_TICKET_OTHER
    if answer.startswith("SELF"):
        if _FORWARDED_SUBJECT_RE.match(subject or ""):
            print("[AUTH BYPASS] Forwarded ticket read as the sender's own, leaving for triage")
            return None
        print("[AUTH BYPASS] Claude detected auth issue")
        return AUTH_TICKET_SELF
    return None


def _run_auth_check(email: str) -> dict | None:
    """Check user auth status via the Django API."""
    # Prefer the explicit prod URL; fall back to DJANGO_API_URL for local dev.
    django_url = os.environ.get("DJANGO_PROD_API_URL") or os.environ.get(
        "DJANGO_API_URL", ""
    )
    api_key = os.environ.get("SUPPORT_API_KEY", "")
    if not django_url or not api_key:
        print("[AUTH] DJANGO_PROD_API_URL/DJANGO_API_URL or SUPPORT_API_KEY not set")
        return None
    try:
        resp = httpx.get(
            f"{django_url}/api/support/auth-check/",
            params={"email": email.strip()},
            headers={"X-Support-Api-Key": api_key},
            timeout=10,
        )
        if resp.status_code != 200:
            # 403 (bad/missing key), 5xx, etc. NEVER treat this as a result --
            # the error body (e.g. {"error": "Unauthorized"}) has no `found`
            # key and would otherwise fall through to "account not found".
            print(
                f"[AUTH] Check {email}: HTTP {resp.status_code} "
                f"-- treating as unknown (body: {resp.text[:200]})"
            )
            return None
        result = resp.json()
        if not isinstance(result, dict) or "found" not in result:
            # Malformed/unexpected payload -- don't guess.
            print(f"[AUTH] Check {email}: unexpected payload {str(result)[:200]}")
            return None
        print(
            f"[AUTH] Check {email}: "
            f"found={result.get('found')}, "
            f"active={result.get('is_active')}, "
            f"bypassable={result.get('is_bypassable')}"
        )
        return result
    except Exception as e:
        print(f"[AUTH] Check failed for {email}: {e}")
        return None


def _run_auth_bypass(email: str) -> dict | None:
    """Activate a user's account via the Django API."""
    django_url = os.environ.get("DJANGO_PROD_API_URL") or os.environ.get(
        "DJANGO_API_URL", ""
    )
    api_key = os.environ.get("SUPPORT_API_KEY", "")
    if not django_url or not api_key:
        return None
    try:
        resp = httpx.post(
            f"{django_url}/api/support/auth-check/",
            json={"email": email.strip(), "action": "bypass"},
            headers={"X-Support-Api-Key": api_key},
            timeout=10,
        )
        if resp.status_code != 200:
            print(
                f"[AUTH] Bypass {email}: HTTP {resp.status_code} "
                f"-- not activated (body: {resp.text[:200]})"
            )
            return None
        result = resp.json()
        if not isinstance(result, dict) or "bypassed" not in result:
            print(f"[AUTH] Bypass {email}: unexpected payload {str(result)[:200]}")
            return None
        print(f"[AUTH] Bypass {email}: bypassed={result.get('bypassed')}")
        return result
    except Exception as e:
        print(f"[AUTH] Bypass failed for {email}: {e}")
        return None


def _send_auth_reply(ticket_id: str, to_email: str, content: str) -> None:
    """Send an auth-specific reply via ZohoDesk email."""
    body = {
        "channel": "EMAIL",
        "fromEmailAddress": ZOHO_FROM_ADDRESS,
        "to": to_email,
        "content": content,
        "contentType": "plainText",
    }
    cc_email = fetch_ticket_cc(ticket_id)
    if cc_email:
        body["cc"] = cc_email
    _zoho_desk_call("ZohoDesk_sendReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })


def _auth_bypass_first_name(contact_name: str) -> str:
    """Extract a usable first name from contact_name, or return 'there'."""
    parts = (contact_name or "").strip().split()
    if len(parts) >= 2 and parts[0].isalpha() and 2 <= len(parts[0]) <= 15:
        return parts[0]
    return "there"


def _handle_auth_bypass_email_ticket(
    ticket_id: str,
    contact_email: str,
    contact_name: str,
    detected_lang: str | None,
) -> bool:
    """Run auth check and send the appropriate reply for an auth code ticket.

    Returns True if the ticket was handled (reply sent) and normal processing
    should be skipped. Returns False if the check failed and the ticket should
    fall through to the standard pipeline.

    VOICE: these replies are signed "Vome Support" / "Equipe Vome", so the
    copy speaks as the team ("we checked", "nous avons"), never as one
    person ("I checked", "j'ai"). Keep any new branch plural.
    """
    auth_result = _run_auth_check(contact_email)
    if auth_result is None:
        return False

    is_french = detected_lang == "French"
    name = _auth_bypass_first_name(contact_name)

    if auth_result.get("is_bypassable"):
        bypass_result = _run_auth_bypass(contact_email)
        if not (bypass_result and bypass_result.get("bypassed")):
            return False
        if is_french:
            reply = (
                f"Bonjour {name},\n\n"
                "Bonne nouvelle, nous avons activé votre compte. "
                "Vous devriez maintenant pouvoir vous connecter.\n\n"
                "Si vous avez encore des difficultés, vous pouvez "
                "réinitialiser votre mot de passe ici :\n"
                "https://www.vomevolunteer.com/forgot\n\n"
                "Cordialement,\nÉquipe Vome"
            )
        else:
            reply = (
                f"Hi {name},\n\n"
                "Good news, we were able to activate your account. "
                "You should be able to log in now.\n\n"
                "If you still have trouble signing in, you can "
                "reset your password here:\n"
                "https://www.vomevolunteer.com/forgot\n\n"
                "Best,\nVome Support"
            )
        _send_auth_reply(ticket_id, contact_email, reply)
        print(f"[AUTH BYPASS] Account activated, reply sent — ticket {ticket_id}")
        return True

    if auth_result.get("is_active"):
        if is_french:
            reply = (
                f"Bonjour {name},\n\n"
                "Nous avons vérifié votre compte et il est déjà actif. "
                "Il semble que votre autorisation par e-mail ait "
                "déjà été traitée.\n\n"
                "Si vous avez du mal à vous connecter, essayez de "
                "réinitialiser votre mot de passe ici :\n"
                "https://www.vomevolunteer.com/forgot\n\n"
                "Cordialement,\nÉquipe Vome"
            )
        else:
            reply = (
                f"Hi {name},\n\n"
                "We checked your account and it's already active. "
                "It looks like your email authorization may have "
                "already gone through.\n\n"
                "If you're having trouble signing in, try resetting "
                "your password here:\n"
                "https://www.vomevolunteer.com/forgot\n\n"
                "Best,\nVome Support"
            )
        _send_auth_reply(ticket_id, contact_email, reply)
        print(f"[AUTH BYPASS] Already active, reply sent — ticket {ticket_id}")
        return True

    # Offline/created profile — the account exists (address on alternate_email,
    # UUID username, unusable password) but isn't claimed. Can't auto-activate;
    # tell them to claim it by registering with this same email so the merge
    # flow links their existing profile.
    if auth_result.get("is_offline_profile"):
        if is_french:
            reply = (
                f"Bonjour {name},\n\n"
                "Bonne nouvelle, votre profil bénévole existe déjà dans "
                "notre système. Il a été créé par votre organisation, il "
                "vous suffit donc de l'activer en créant vos identifiants "
                "de connexion.\n\n"
                f"Veuillez vous inscrire avec cette même adresse e-mail "
                f"({contact_email}) ici :\n"
                "https://www.vomevolunteer.com/register-volunteer\n\n"
                "Une fois inscrit avec cette adresse, votre profil et votre "
                "historique existants seront automatiquement liés.\n\n"
                "Si vous rencontrez le moindre problème, répondez simplement "
                "à ce message et nous vous aiderons.\n\n"
                "Cordialement,\nÉquipe Vome"
            )
        else:
            reply = (
                f"Hi {name},\n\n"
                "Good news, your volunteer profile already exists in our "
                "system. It was set up by your organization, so you just "
                "need to claim it by creating your login.\n\n"
                f"Please register using this same email address "
                f"({contact_email}) here:\n"
                "https://www.vomevolunteer.com/register-volunteer\n\n"
                "Once you sign up with this email, your existing profile and "
                "activity will be linked automatically.\n\n"
                "If you run into any trouble, just reply here and we'll "
                "help.\n\n"
                "Best,\nVome Support"
            )
        _send_auth_reply(ticket_id, contact_email, reply)
        print(f"[AUTH BYPASS] Offline profile, claim reply sent — ticket {ticket_id}")
        return True

    # Account not found — only send the definitive "no account" reply when the
    # backend explicitly told us found=False. Any other state (found=True but no
    # branch matched, or a malformed result) is ambiguous: fall through to the
    # normal pipeline rather than tell a real user their account doesn't exist.
    if auth_result.get("found") is not False:
        print(
            f"[AUTH BYPASS] Ambiguous auth result, not sending not-found "
            f"reply — ticket {ticket_id}: {str(auth_result)[:200]}"
        )
        return False

    # Account not found — reply and return True so we skip the generic ack/ClickUp
    if is_french:
        reply = (
            f"Bonjour {name},\n\n"
            "Nous n'avons pas trouvé de compte associé à cette adresse "
            "e-mail. Il est possible que vous ayez utilisé une "
            "adresse différente lors de votre inscription.\n\n"
            "Si vous n'êtes pas encore inscrit, vous pouvez le "
            "faire ici :\n"
            "https://www.vomevolunteer.com/register-volunteer\n\n"
            "Si vous pensez qu'il y a une erreur, répondez avec "
            "l'adresse e-mail que vous avez utilisée lors de votre "
            "inscription et nous vérifierons.\n\n"
            "Cordialement,\nÉquipe Vome"
        )
    else:
        reply = (
            f"Hi {name},\n\n"
            "We weren't able to find an account registered with "
            "this email address. It's possible you signed up "
            "with a different email.\n\n"
            "If you haven't registered yet, you can do so here:\n"
            "https://www.vomevolunteer.com/register-volunteer\n\n"
            "If you think this is a mistake, please reply with "
            "the email address you used to sign up and we'll "
            "look into it.\n\n"
            "Best,\nVome Support"
        )
    _send_auth_reply(ticket_id, contact_email, reply)
    print(f"[AUTH BYPASS] Account not found, reply sent — ticket {ticket_id}")
    return True


# Accounts that manage an organization's volunteers and can resend invites.
_ADMIN_USER_TYPES = {"organization_admin", "school_admin", "school_board_admin"}

_INVITE_FAQ_URL = {
    "en": "https://support.vomevolunteer.com/portal/en/kb/articles/what-happens-when-i-invite-volunteers-to-claim-their-profile-on-vome",
    "fr": "https://support.vomevolunteer.com/portal/fr/kb/articles/que-se-passe-t-il-lorsque-j-invite-des-b%C3%A9n%C3%A9voles-%C3%A0-r%C3%A9clamer-leur-profil-sur-vome",
}
_LOGIN_HELP_FAQ_URL = {
    "en": "https://support.vomevolunteer.com/portal/en/kb/articles/my-volunteer-is-having-trouble-logging-into-their-account-how-can-i-help-them",
    "fr": "https://support.vomevolunteer.com/portal/fr/kb/articles/un-b%C3%A9n%C3%A9vole-a-des-difficult%C3%A9s-%C3%A0-se-connecter-%C3%A0-son-compte-comment-puis-je-l-aider",
}


def _handle_admin_signin_help_ticket(
    ticket_id: str,
    contact_email: str,
    contact_name: str,
    detected_lang: str | None,
) -> bool:
    """Send the invitation FAQ to an admin writing about a volunteer's sign-in.

    Only when the sender's own account is an active admin: a volunteer
    writing about a friend, or an unknown sender, gets no admin instructions
    and falls through to triage. Wording is Sam's standard reply (#9549).
    """
    auth_result = _run_auth_check(contact_email)
    if not (
        auth_result
        and auth_result.get("is_active")
        and auth_result.get("user_type") in _ADMIN_USER_TYPES
    ):
        print(
            f"[AUTH BYPASS] Sign-in problem for someone else, but sender is "
            f"not an active admin, leaving for triage -- ticket {ticket_id}: "
            f"{str(auth_result)[:200]}"
        )
        return False

    name = _auth_bypass_first_name(contact_name)
    if detected_lang == "French":
        reply = (
            f"Bonjour {name},\n\n"
            "Si une personne ne reçoit pas le courriel d'invitation pour "
            "une raison quelconque, vous pouvez simplement lui envoyer un "
            "lien d'inscription. L'important est qu'elle s'inscrive avec la "
            "même adresse courriel que celle à laquelle vous l'avez "
            "invitée.\n\n"
            "Que se passe-t-il lorsque j'invite des bénévoles à réclamer "
            "leur profil sur Vome ?\n"
            f"{_INVITE_FAQ_URL['fr']}\n\n"
            "Si elle s'inscrit et a de la difficulté à se connecter pour "
            "une raison quelconque :\n"
            "Un bénévole a des difficultés à se connecter à son compte. "
            "Comment puis-je l'aider ?\n"
            f"{_LOGIN_HELP_FAQ_URL['fr']}\n\n"
            "N'hésitez pas à nous écrire si vous rencontrez d'autres "
            "difficultés !\n\n"
            "Cordialement,\nÉquipe Vome"
        )
    else:
        reply = (
            f"Hi {name},\n\n"
            "If a user does not receive the email invitation for any "
            "reason, you can simply send a signup link to them. All that "
            "matters is that they sign up with the same email address that "
            "you invited them from.\n\n"
            "What happens when I invite volunteers to claim their profile "
            "on Vome?\n"
            f"{_INVITE_FAQ_URL['en']}\n\n"
            "If they do sign up and have any trouble logging in for any "
            "reason:\n"
            "A volunteer is having trouble logging into their account. How "
            "can I help them?\n"
            f"{_LOGIN_HELP_FAQ_URL['en']}\n\n"
            "Let us know if there is any additional difficulty here!\n\n"
            "Best,\nVome Support"
        )
    _send_auth_reply(ticket_id, contact_email, reply)
    print(f"[AUTH BYPASS] Admin asked about a volunteer, invitation FAQ sent -- ticket {ticket_id}")
    return True


def _get_latest_client_reply(conversations_result, contact_email: str) -> str:
    """Return the most recent client message text (HTML stripped, max 200 chars).

    Returns empty string if there is only one conversation entry or no client
    reply is found — caller should only display this when non-empty.
    """
    if not conversations_result:
        return ""

    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list) or len(data) < 2:
        return ""

    contact_email_lower = contact_email.lower()

    for entry in data:
        if not entry.get("isPublic", True):
            continue
        content = entry.get("content", "") or ""
        if NOTE_HEADER in content or UPDATE_HEADER in content:
            continue

        author = entry.get("author", {}) or {}
        author_email = (author.get("email") or "").lower()
        author_type = (author.get("type") or "").upper()

        is_client = (
            author_type == "END_USER"
            or (author_email and author_email == contact_email_lower)
            or (author_email and author_email not in TEAM_EMAILS and author_type != "AGENT")
        )
        if not is_client:
            continue

        clean = re.sub(r"<[^>]+>", "", content).strip()
        if not clean:
            continue

        return clean[:200] + ("..." if len(clean) > 200 else "")

    return ""


# In-memory dedup: tracks ticket IDs currently being processed or recently
# completed.  Prevents duplicate processing when Zoho fires the webhook
# more than once (network retry, timeout, etc.).
_processing_tickets: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 300  # ignore duplicate webhooks within 5 minutes

def _dedup_check(ticket_id: str) -> bool:
    """Return True if this ticket should be skipped (already processing)."""
    import time
    now = time.time()
    # Clean expired entries
    expired = [
        k for k, v in _processing_tickets.items()
        if now - v > _DEDUP_TTL_SECONDS
    ]
    for k in expired:
        del _processing_tickets[k]
    if ticket_id in _processing_tickets:
        return True
    _processing_tickets[ticket_id] = now
    return False


def _has_agent_reply(conversations_result) -> bool:
    """Check if the agent has already replied to this ticket.

    Returns True if any outbound message from our team exists in the
    conversation history.  This prevents sending duplicate auto-acks
    even if the in-memory dedup is bypassed (e.g. server restart).
    """
    if not conversations_result:
        return False
    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        return False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("isDescriptionThread"):
            continue
        author = entry.get("author", {}) or {}
        if (author.get("type") or "").upper() == "AGENT":
            return True
        if entry.get("direction") == "out":
            return True
    return False


def process_ticket(ticket_data: dict) -> str | None:
    """New-ticket fast path. Shadow triage handles everything else.

    Legacy intake (classifier, CRM tier, Zoho auto-assignment, auto-ack,
    ClickUp queue task, #vome-tickets brief) was removed 2026-10-18: the
    sprint thread card and Sam's `route` replace it. What is left is the
    activation-code fix, which resolves a blocked login on its own.
    """
    ticket_id = ticket_data.get("ticket_id", "unknown")

    # Dedup: skip if already processing or recently processed
    if _dedup_check(ticket_id):
        print(f"Duplicate webhook for ticket {ticket_id} -- skipping")
        return None

    try:
        zoho_ticket = fetch_ticket_from_zoho(ticket_id)
        conversations_result = fetch_ticket_conversations(ticket_id)

        # Safety: skip if we've already replied to this ticket
        if _has_agent_reply(conversations_result):
            print(f"Agent already replied to ticket {ticket_id} -- skipping to prevent duplicate")
            return None

        if zoho_ticket:
            fields = _extract_ticket_fields(zoho_ticket)
            contact_name = fields["contact_name"]
            contact_email = fields["contact_email"]
            subject = fields["subject"]
            body = fields["description"]
        else:
            # Fall back to webhook payload
            contact_name = ticket_data.get("contact_name", "")
            contact_email = ticket_data.get("contact_email", "")
            subject = ticket_data.get("subject", "")
            body = ticket_data.get("body", "")
            print(f"Zoho fetch failed -- falling back to webhook payload for ticket {ticket_id}")

        # Error reports from Vome frontend -- Sam handles these directly
        if is_error_report(subject, body):
            print(f"Vome Error Report detected in ticket {ticket_id} -- leaving as New for Sam")
            return None

        # Auth fast path: a sender who can't get into their own account is
        # checked and activated now; an admin writing about a volunteer's
        # sign-in gets the invitation FAQ instead.
        auth_kind = _classify_auth_ticket(subject, body)
        if auth_kind is None:
            return None
        thread_text = _format_conversations(conversations_result)
        detected_lang = _detect_language(body) or _detect_language(thread_text)
        if auth_kind == AUTH_TICKET_OTHER:
            handled = _handle_admin_signin_help_ticket(
                ticket_id=ticket_id,
                contact_email=contact_email,
                contact_name=contact_name,
                detected_lang=detected_lang,
            )
            return "admin_signin_help_sent" if handled else None
        handled = _handle_auth_bypass_email_ticket(
            ticket_id=ticket_id,
            contact_email=contact_email,
            contact_name=contact_name,
            detected_lang=detected_lang,
        )
        return "auth_bypass_handled" if handled else None
    except Exception as e:
        print(f"Agent error processing ticket {ticket_id}: {e}")
        return None


def _find_clickup_task_by_zoho_ticket(ticket_id: str) -> str | None:
    """Fallback locator: find a ClickUp task by its 'Zoho Ticket Link' field.

    Used when the thread_map row has no clickup_task_id (older or
    manually-created tasks). Matches by the Zoho ticket *ID substring*, NOT the
    full URL, because the stored link can use either the desk.zoho.com domain
    (agent-created) or the white-labeled support.vomevolunteer.com domain
    (manually pasted) -- only the trailing ticket ID is stable across both.

    Scans the workspace's tasks (the filtered team-tasks endpoint already used
    in slack_digest.py) including closed ones. Returns the task id or None.
    """
    import httpx as _httpx

    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    team_id = os.environ.get("CLICKUP_TEAM_ID", "")
    if not cu_token or not team_id or not ticket_id:
        print(
            f"[ZOHO->CU] cannot run ClickUp link fallback "
            f"(token/team_id/ticket_id missing) for ticket {ticket_id}"
        )
        return None

    field_id = "4776215b-c725-4d79-8f20-c16f0f0145ac"  # Zoho Ticket Link
    headers = {"Authorization": cu_token}
    scanned = 0
    try:
        for page in range(15):  # safety cap (~1500 tasks)
            r = _httpx.get(
                f"https://api.clickup.com/api/v2/team/{team_id}/task",
                params={
                    "include_closed": "true",
                    "subtasks": "true",
                    "page": page,
                },
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
            tasks = r.json().get("tasks", [])
            if not tasks:
                break
            for t in tasks:
                scanned += 1
                for f in t.get("custom_fields") or []:
                    if f.get("id") != field_id:
                        continue
                    if ticket_id in str(f.get("value") or ""):
                        print(
                            f"[ZOHO->CU] matched ClickUp task {t.get('id')} "
                            f"to ticket {ticket_id} via Zoho Ticket Link "
                            f"field (scanned {scanned})"
                        )
                        return t.get("id")
            if len(tasks) < 100:  # last page
                break
        print(
            f"[ZOHO->CU] no ClickUp task references ticket {ticket_id} "
            f"(scanned {scanned} tasks)"
        )
        return None
    except Exception as e:
        print(
            f"[ZOHO->CU] ClickUp lookup by Zoho Ticket Link failed "
            f"({ticket_id}): {e}"
        )
        return None


def _close_queue_tasks_if_closed(ticket_id: str,
                                 status_known: bool = False) -> None:
    """Close every open Priority Queue task linked to a Closed ticket, with
    the engineers taken off and a comment (ticket_reconciler.close_task)."""
    try:
        import ticket_reconciler
        import zoho_actions

        if not status_known and \
                zoho_actions.ticket_status(ticket_id) != ZOHO_CLOSED:
            return
        closed = ticket_reconciler.close_tasks_for_ticket(ticket_id)
        if closed:
            print(f"[ZOHO->CU] ticket {ticket_id} Closed -- closed "
                  f"{', '.join(closed)}")
    except Exception as e:
        print(f"[ZOHO->CU] closing tasks for {ticket_id} failed: {e}")


def sync_zoho_to_clickup(ticket_id: str) -> None:
    """Sync a Zoho ticket's status/assignee changes to ClickUp.

    Called when a Zoho ticket update webhook fires. Handles:
    - Ticket closed on Zoho -> close the ClickUp task
    - Ticket set to Awaiting Client Response on Zoho -> set the ClickUp task
      to 'awaiting client response' (parked, not closed)
    - Ticket reassigned to Sam/Ron or unassigned -> close ClickUp task
    """
    from database import get_thread_by_ticket_id, update_thread

    print(f"[ZOHO->CU] sync_zoho_to_clickup called for ticket {ticket_id}")

    # Look up the ClickUp task ID from our database
    thread_info = get_thread_by_ticket_id(ticket_id)
    if not thread_info:
        print(
            f"[ZOHO->CU] no thread_map row for ticket {ticket_id} -- "
            f"cannot sync (ticket may predate the link, or never got a "
            f"ClickUp task). Skipping."
        )
        # A ticket Sam closed still has no engineer task left (2026-10-24):
        # find them by their Zoho Ticket Link instead.
        _close_queue_tasks_if_closed(ticket_id)
        return
    thread_ts, thread_data = thread_info
    clickup_task_id = thread_data.get("clickup_task_id")
    if not clickup_task_id:
        # Safety fallback: the DB row never recorded the task ID (older or
        # manually-created task). Locate it by the ClickUp "Zoho Ticket Link"
        # custom field, then backfill the row so future syncs are instant.
        print(
            f"[ZOHO->CU] thread {thread_ts} for ticket {ticket_id} has no "
            f"clickup_task_id stored -- searching ClickUp by Zoho Ticket Link."
        )
        clickup_task_id = _find_clickup_task_by_zoho_ticket(ticket_id)
        if not clickup_task_id:
            print(
                f"[ZOHO->CU] no linked ClickUp task found for ticket "
                f"{ticket_id} -- cannot sync. Skipping."
            )
            return
        update_thread(thread_ts, clickup_task_id=clickup_task_id)
        print(
            f"[ZOHO->CU] backfilled thread {thread_ts} with "
            f"clickup_task_id={clickup_task_id}"
        )

    # Fetch current ticket state from Zoho
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    if not zoho_ticket:
        print(
            f"[ZOHO->CU] fetch_ticket_from_zoho returned nothing for "
            f"ticket {ticket_id} -- cannot read status. Skipping."
        )
        return
    ticket = _unwrap_mcp_result(zoho_ticket) or {}
    status = (ticket.get("status") or "").lower().strip()
    assignee_id = ticket.get("assigneeId") or ""

    print(
        f"[ZOHO->CU] ticket {ticket_id}: status={status!r} "
        f"assignee_id={assignee_id!r} clickup_task_id={clickup_task_id} "
        f"(expecting closed={ZOHO_CLOSED.lower()!r}/"
        f"{ZOHO_RESOLVED.lower()!r}, "
        f"awaiting={ZOHO_AWAITING_CLIENT_RESPONSE.lower()!r})"
    )

    # Closed on Zoho -> close on ClickUp
    if status in (ZOHO_CLOSED.lower(), ZOHO_RESOLVED.lower()):
        print(f"[ZOHO->CU] ticket {ticket_id} is {status} -- closing ClickUp task {clickup_task_id}")
        close_clickup_task(clickup_task_id)
        update_thread(thread_ts, status=THREAD_CLOSED)
        # A second Priority Queue task for the same ticket goes too.
        _close_queue_tasks_if_closed(ticket_id, status_known=True)
        return

    # Awaiting Client Response on Zoho -> mirror on ClickUp (parked).
    # Checked BEFORE the assignee rules below so the task is parked rather
    # than closed even when the ticket is owned by Sam/Ron.
    if status == ZOHO_AWAITING_CLIENT_RESPONSE.lower():
        print(
            f"[ZOHO->CU] ticket {ticket_id} awaiting client response -- "
            f"ClickUp task {clickup_task_id} -> {CU_AWAITING_CLIENT}"
        )
        _update_clickup_task_status(clickup_task_id, CU_AWAITING_CLIENT)
        update_thread(thread_ts, status=THREAD_WAITING_CLIENT)
        return

    # An agent fix sits on the Agent Fixes team with no agent on purpose
    # (zoho_actions.set_agent_fixing): the owner rules below would read
    # that as "unassigned" and close the task the release watcher needs.
    from zoho_actions import AGENT_FIXES_TEAM

    if str(ticket.get("teamId") or "") == AGENT_FIXES_TEAM:
        print(
            f"[ZOHO->CU] ticket {ticket_id} is on the Agent Fixes team -- "
            f"ClickUp task {clickup_task_id} left as is"
        )
        return

    # Reassigned to Sam or Ron (not an engineer) -> close ClickUp task
    engineer_ids = {ZOHO_AGENT_SANJAY, ZOHO_AGENT_ONLYG}
    if assignee_id and assignee_id not in engineer_ids:
        print(
            f"[ZOHO->CU] ticket {ticket_id} reassigned to non-engineer "
            f"({assignee_id}) -- closing ClickUp task {clickup_task_id}"
        )
        close_clickup_task(clickup_task_id)
        return

    # Unassigned -> also close ClickUp (Sam will handle)
    if not assignee_id:
        print(
            f"[ZOHO->CU] ticket {ticket_id} unassigned -- "
            f"closing ClickUp task {clickup_task_id}"
        )
        close_clickup_task(clickup_task_id)
        return

    # No rule matched: status isn't closed/resolved/awaiting and the ticket
    # is still owned by an engineer. This is the silent no-op that looks like
    # "nothing happened" -- log it explicitly so we can see the status string
    # that fell through (e.g. a renamed/recased Zoho status).
    print(
        f"[ZOHO->CU] ticket {ticket_id} status={status!r} did not match any "
        f"sync rule and is still owned by an engineer ({assignee_id!r}) -- "
        f"no ClickUp change. If you expected one, the Zoho status string "
        f"above probably doesn't match the expected values logged earlier."
    )


def _is_client_reply(conversations_result: dict | None, ticket_id: str = "unknown") -> tuple[bool, dict]:
    """Check if the most recent conversation entry is a client reply.

    Returns (is_client_reply, latest_entry) so callers can inspect the author.
    """
    empty = (False, {})
    if not conversations_result:
        return empty

    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list) or not data:
        return empty

    latest = data[0]

    # Skip agent's own output
    content = latest.get("content", "") or ""
    if NOTE_HEADER in content or UPDATE_HEADER in content:
        print(f"Ticket update ignored -- agent's own note (ticket {ticket_id})")
        return (False, latest)

    author = latest.get("author", {}) or {}
    author_email = (author.get("email") or "").lower()
    author_type = (author.get("type") or "").upper()
    visibility = "public" if latest.get("isPublic", True) else "private"

    # A reply is from a client when:
    #   1. author.type is END_USER, OR
    #   2. visibility is public AND author is not a known team member
    is_end_user = author_type == "END_USER"
    is_public_non_team = visibility == "public" and author_email not in TEAM_EMAILS

    if is_end_user or is_public_non_team:
        print(f"Client reply detected from {author_email} on ticket {ticket_id} -- processing")
        return (True, latest)

    reason = f"author.type={author_type}, visibility={visibility}, email={author_email}"
    print(f"Ticket update ignored -- not a client reply ({reason}) (ticket {ticket_id})")
    return (False, latest)


def is_zoho_reply_event(
    event_type: str, raw_id: str, raw_ticket_id: str
) -> bool:
    """True only when a Zoho ticket-update webhook represents a NEW client
    reply (a thread-add), not a ticket-field change (status/assignee edit).

    Why this gate exists: the update handler runs reply-handling, which
    re-derives "the client replied" from the *latest* conversation entry. So a
    bare field change -- e.g. Sam manually closing a ticket whose last message
    happens to be from the client -- would be mistaken for a reply and have its
    status restored, fighting him in a Closed<->Processing loop.

    Signals (either suffices, so a genuine reply is never missed):
      * eventType mentions "thread" (e.g. Ticket_Thread_Add), or
      * the reply ID is in `id` while the real ticket ID is in `ticketId`
        (so `ticketId` is present and differs from `id`). Ticket-level events
        carry only `id`.
    """
    if "thread" in (event_type or "").lower():
        return True
    return raw_ticket_id != "" and raw_ticket_id != raw_id


def _extract_reply_text(latest: dict) -> str:
    """Best-effort plain text of a client reply thread.

    CRITICAL: the conversations *list* endpoint returns reply threads with a
    `summary` field but NO `content` (only comments carry `content`). Reading
    `content` alone therefore yields an empty string for every client reply,
    which blinds the no-action / ack classifiers and the action-signal guards —
    they end up judging "(no text content)" and can misfire (e.g. wrongly
    re-closing a "it's still broken" reply). Fall back to `summary` and strip
    HTML so the guards see the words the client actually wrote. Mirrors the
    content->summary fallback already used in `_format_conversations`.
    """
    raw = latest.get("content") or latest.get("summary") or ""
    return re.sub(r"<[^>]+>", "", raw).strip()


def _classify_client_reply(reply_content: str) -> dict:
    """Classify a client reply as acknowledgment or substantive.

    Returns {"type": "ack"|"substantive", "summary": str}.
    - ack: simple thank-you, got it, ok, thumbs-up ��� no new info
    - substantive: answers a question, provides details, screenshots,
      steps, emails, new context the engineer needs
    """
    prompt = (
        "Classify this client reply to a support ticket.\n\n"
        f"Reply:\n\"{reply_content}\"\n\n"
        "Return valid JSON only:\n"
        "{\n"
        '  "type": "ack" or "substantive",\n'
        '  "summary": "one-line summary of what the client said"\n'
        "}\n\n"
        "Rules:\n"
        "- ack: thank you, thanks, got it, ok, sounds good,"
        " will do, perfect, great, acknowledged, any simple"
        " confirmation with no new information\n"
        "- substantive: provides new details, answers a"
        " question, shares steps to reproduce, mentions"
        " affected users/emails, describes behavior,"
        " provides screenshots, adds context the team needs"
    )
    try:
        response = client.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        import json as _json
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return _json.loads(raw)
    except Exception as e:
        print(f"Reply classification failed: {e}")
        # Default to substantive to be safe
        return {"type": "substantive", "summary": ""}


def _update_clickup_task_status(task_id: str, status: str) -> bool:
    """Update a ClickUp task's status (e.g. QUEUED, IN PROGRESS)."""
    import httpx as _httpx
    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not cu_token or not task_id:
        return False
    try:
        r = _httpx.put(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            json={"status": status},
            headers={
                "Authorization": cu_token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"ClickUp task {task_id} status set to {status}")
        return True
    except Exception as e:
        print(f"ClickUp status update failed ({task_id}): {e}")
        return False


def _append_clickup_task_context(
    task_id: str, new_context: str
) -> bool:
    """Append new context to a ClickUp task's description."""
    import httpx as _httpx
    from datetime import datetime, timezone
    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not cu_token or not task_id:
        return False
    try:
        # Fetch current description
        r = _httpx.get(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            headers={"Authorization": cu_token},
            timeout=15,
        )
        r.raise_for_status()
        current_desc = r.json().get("description", "")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        updated = (
            f"{current_desc}\n\n"
            f"[Client reply — {date_str}]: {new_context}"
        )
        r2 = _httpx.put(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            json={"description": updated},
            headers={
                "Authorization": cu_token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r2.raise_for_status()
        print(f"ClickUp task {task_id} description updated")
        return True
    except Exception as e:
        print(f"ClickUp description update failed ({task_id}): {e}")
        return False


def _add_clickup_comment(task_id: str, text: str) -> bool:
    """Post a comment on a ClickUp task (used to log the client's reply)."""
    import httpx as _httpx
    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not cu_token or not task_id:
        return False
    try:
        r = _httpx.post(
            f"https://api.clickup.com/api/v2/task/{task_id}/comment",
            json={"comment_text": text, "notify_all": False},
            headers={
                "Authorization": cu_token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"ClickUp comment added to task {task_id}")
        return True
    except Exception as e:
        print(f"ClickUp comment failed ({task_id}): {e}")
        return False


def _set_zoho_ticket_status(ticket_id: str, status: str) -> bool:
    """Update the Zoho ticket status."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": status},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(
            f"Failed to set Zoho ticket {ticket_id}"
            f" to {status}"
        )
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(
            f"Zoho status update failed for ticket"
            f" {ticket_id}: {data}"
        )
        return False
    print(f"Zoho ticket {ticket_id} status set to {status}")
    return True


# ---------------------------------------------------------------------------
# No-action client replies (courtesy acks) — realign Zoho to mirror ClickUp.
# NO email is ever sent here; ClickUp is left exactly as-is.
# ---------------------------------------------------------------------------

# Out-of-office / autoresponder guard. A match means we do NOT treat the
# message as a genuine client reply (leave the ticket untouched for a human).
_AUTO_REPLY_RE = re.compile(
    r"(?i)("
    r"out of (the )?office|auto(?:matic)?[ -]?repl|away from (my )?(desk|office)"
    r"|on (vacation|holiday|leave|annual leave|parental leave|maternity leave)"
    r"|currently (away|out of (the )?office)|i am (currently )?out of"
    r"|i will be out of|réponse automatique|absence du bureau|de retour le"
    r"|mailbox (?:is|will) not (?:be )?(?:attended|monitored)"
    r"|en (congé|vacances)"
    r")"
)

# Obvious action signals. ANY match forces action-needed regardless of the
# model. Precision over recall: a false action-needed is safe (it just falls
# through to the normal flow); a false no-action could wrongly realign status.
_ACTION_SIGNAL_RE = re.compile(
    r"(?i)("
    r"\?|\bbut\b|\balso\b|\bstill\b|not working|\bdoesn'?t\b|\bdidn'?t\b"
    r"|\bisn'?t\b|\bcan'?t\b|\bcannot\b|\bwon'?t\b|\berror\b|\bissue\b"
    r"|\bproblem\b|\bbroken\b|\bfail|\bcrash|\bhow\b|\bwhen\b|\bwhy\b"
    r"|\bwhere\b|can you|could you|\bhelp\b|\bunable\b|\bwrong\b|\bagain\b"
    r")"
)


def _looks_like_auto_reply(text: str) -> bool:
    """True if the text looks like an out-of-office / autoresponder."""
    return bool(_AUTO_REPLY_RE.search(text or ""))


# Zoho's "Customer Reply to Processing Status" workflow moves a ticket to
# Processing on ANY customer email, an out-of-office included. The webhook
# can reach us before that workflow has run, so the restore waits a moment.
AUTO_REPLY_SETTLE_SECONDS = 20
# Clock skew between the reply's createdTime and the workflow's eventTime
# (#9526: reply 10:38:22, workflow 10:38:31).
AUTO_REPLY_WORKFLOW_SKEW_SECONDS = 120


def _zoho_time(value: str):
    from datetime import datetime

    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _restore_status_after_auto_reply(ticket_id: str, reply: dict) -> str | None:
    """Put back the status Zoho's reply workflow overwrote for an
    out-of-office. Returns the restored status, or None if nothing changed.

    #9526 (2026-10-20): our answer closed the ticket, Allie's out-of-office
    arrived 10 seconds later, the workflow flipped Closed -> Processing, and
    the ticket sat there looking owed for days. Only the newest status change
    is undone, and only when a workflow made it, to Processing, around the
    time of the auto-reply: a status a person set since is never overridden.
    """
    from datetime import timedelta
    from zoho_desk_api import status_history

    changes = status_history(ticket_id)
    if not changes:
        print(f"[NO-ACTION] OOO on ticket {ticket_id}: no status history "
              "read -- status left as is")
        return None
    newest = changes[0]
    replied = _zoho_time(reply.get("createdTime") or "")
    changed = _zoho_time(newest["time"])
    by_reply_workflow = (
        newest["actor_type"].lower() == "workflow"
        and newest["to"].lower() == ZOHO_PROCESSING.lower()
        and replied is not None and changed is not None
        and changed >= replied - timedelta(
            seconds=AUTO_REPLY_WORKFLOW_SKEW_SECONDS)
    )
    if not by_reply_workflow or not newest["from"] \
            or newest["from"].lower() == newest["to"].lower():
        print(f"[NO-ACTION] OOO on ticket {ticket_id}: newest status change "
              f"({newest['actor']}: {newest['from']} -> {newest['to']}) is "
              "not the reply workflow -- status left as is")
        return None
    if not _set_zoho_ticket_status(ticket_id, newest["from"]):
        return None
    print(f"[NO-ACTION] OOO on ticket {ticket_id}: status restored "
          f"{newest['to']} -> {newest['from']}")
    return newest["from"]


def _restore_status_after_auto_reply_later(ticket_id: str, reply: dict) -> None:
    """_restore_status_after_auto_reply once Zoho's workflow has run. The
    webhook handler runs inline, so this never sleeps on it."""
    import threading

    def _run():
        try:
            _restore_status_after_auto_reply(ticket_id, reply)
        except Exception as exc:
            print(f"[NO-ACTION] OOO status restore failed on ticket "
                  f"{ticket_id}: {exc}")

    timer = threading.Timer(AUTO_REPLY_SETTLE_SECONDS, _run)
    timer.daemon = True
    timer.start()


def _classify_no_action_reply(reply_content: str) -> bool:
    """Cheap Claude classifier: True ONLY for a pure courtesy ack that needs
    no response. Conservative -- returns False on anything substantive or on
    error (precision over recall).
    """
    text = (reply_content or "").strip()
    if not text:
        return False
    prompt = (
        "Classify this client reply to a support ticket.\n\n"
        f"Reply:\n\"{text}\"\n\n"
        "Return valid JSON only: {\"no_action\": true} or "
        "{\"no_action\": false}.\n\n"
        "no_action = true ONLY for a pure acknowledgment or closing "
        "pleasantry that needs no response: 'thanks', 'thank you', 'got it', "
        "'ok', 'will do', 'sounds good', \"I'll check that later\", "
        "'appreciate it', and similar.\n"
        "no_action = false for ANYTHING else: any question, any new "
        "information, any new or continuing problem, anything mentioning it "
        "is still broken / not working / erroring, any request, or any "
        "uncertainty. Precision over recall: when in doubt, false."
    )
    try:
        response = client.messages.create(
            model=SUPPORT_MODEL_FAST,
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return bool(json.loads(raw).get("no_action"))
    except Exception as e:
        print(f"[NO-ACTION] classifier failed: {e}")
        return False


def _is_no_action_reply(reply_content: str, has_attachment: bool) -> bool:
    """Decide if a client reply is a no-action courtesy message.

    Guards force action-needed before trusting the model: an attachment or any
    obvious action signal both mean action-needed.
    """
    if has_attachment:
        return False
    if _ACTION_SIGNAL_RE.search(reply_content or ""):
        return False
    return _classify_no_action_reply(reply_content)


def _is_confident_ack(reply_text: str, has_attachment: bool) -> bool:
    """High-confidence pure courtesy ack — the ONLY thing safe to auto-close a
    resolved ticket on.

    Re-closing a ticket the client just replied to is irreversible from their
    point of view: if we're wrong, the reply is buried and nobody sees it. So
    this is biased hard against closing. It returns True only when ALL hold:
      * we actually have reply text to judge (never close on empty/unreadable
        text — that was the failure that buried a "it's still broken" reply),
      * there are no attachments (screenshots/files = substantive),
      * there is no action signal ("still", "unable", "?", "not working", ...),
      * AND the model also agrees it's a pure ack.
    Anything short of that → not a confident ack → surface it instead.
    """
    text = (reply_text or "").strip()
    if not text:
        return False
    if has_attachment:
        return False
    if _ACTION_SIGNAL_RE.search(text):
        return False
    return _classify_no_action_reply(text)


def _get_clickup_status(task_id: str) -> str:
    """Return the live ClickUp task status (raw string), or '' on failure."""
    import httpx as _httpx
    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not cu_token or not task_id:
        return ""
    try:
        r = _httpx.get(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            headers={"Authorization": cu_token},
            timeout=15,
        )
        r.raise_for_status()
        return (r.json().get("status") or {}).get("status", "") or ""
    except Exception as e:
        print(f"[NO-ACTION] ClickUp get status failed ({task_id}): {e}")
        return ""


def _map_clickup_to_zoho_status(cu_norm: str) -> str | None:
    """Map a NORMALIZED ClickUp status to the Zoho status that mirrors it.

    Matches both new and legacy ClickUp names. Returns None for statuses we
    don't mirror (leave Zoho unchanged).
    """
    if cu_norm in ("closed", CU_DONE):
        return ZOHO_CLOSED
    if cu_norm == CU_ON_PROD:
        return ZOHO_CLOSED
    if cu_norm in (CU_AWAITING_CLIENT, CU_WAITING_ON_CLIENT):
        return ZOHO_AWAITING_CLIENT_RESPONSE
    if cu_norm in (CU_QUEUED, CU_IN_PROGRESS, CU_ON_DEV):
        return ZOHO_IN_PROGRESS
    return None


def _log_auto_handled(
    ticket_id: str, reply_text: str, clickup_state: str, zoho_status: str
) -> None:
    """Log an auto-handled no-action reply to #vome-agent-log."""
    channel = os.environ.get("SLACK_CHANNEL_AGENT_LOG", "")
    if not channel:
        return
    try:
        from slack_sdk import WebClient
        wc = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        preview = " ".join((reply_text or "").split())
        if len(preview) > 200:
            preview = preview[:200] + "..."
        zoho_url = (
            "https://desk.zoho.com/support/vomevolunteer"
            f"/ShowHomePage.do#Cases/dv/{ticket_id}"
        )
        text = (
            ":robot_face: *Auto-handled no-action reply*\n"
            f"Ticket: <{zoho_url}|#{ticket_id}>\n"
            f"Reply: \"{preview or '(no text)'}\"\n"
            f"ClickUp state read: {clickup_state or '(none)'}\n"
            f"Zoho status set: {zoho_status or '(unchanged)'}"
        )
        wc.chat_postMessage(channel=channel, text=text)
    except Exception as e:
        print(f"[NO-ACTION] agent-log post failed: {e}")


def _handle_no_action_reply(ticket_id: str, reply_text: str) -> None:
    """Realign the Zoho status to mirror the linked ClickUp task. No email, no
    draft, no resurface; ClickUp is left exactly as-is. STATUS ONLY -- the
    Zoho owner is never touched.
    """
    from database import get_thread_by_ticket_id, update_thread

    thread_info = get_thread_by_ticket_id(ticket_id)
    thread_ts = None
    clickup_task_id = None
    if thread_info:
        thread_ts, thread_data = thread_info
        clickup_task_id = thread_data.get("clickup_task_id")

    # DB row missing the link? Locate the task by its Zoho Ticket Link field
    # before assuming there's nothing to mirror -- otherwise a courtesy reply
    # on a still-in-progress task would be wrongly closed just because the row
    # never recorded clickup_task_id.
    if not clickup_task_id:
        clickup_task_id = _find_clickup_task_by_zoho_ticket(ticket_id)
        if clickup_task_id and thread_ts:
            update_thread(thread_ts, clickup_task_id=clickup_task_id)
            print(
                f"[NO-ACTION] backfilled thread {thread_ts} with "
                f"clickup_task_id={clickup_task_id}"
            )

    # Still no linked ClickUp task -> Zoho Closed (nothing to mirror)
    if not clickup_task_id:
        _set_zoho_ticket_status(ticket_id, ZOHO_CLOSED)
        print(f"[NO-ACTION] ticket {ticket_id}: no ClickUp task -> Zoho Closed")
        _log_auto_handled(
            ticket_id, reply_text, "(no linked ClickUp task)", ZOHO_CLOSED
        )
        return

    cu_status_raw = _get_clickup_status(clickup_task_id)
    cu_norm = normalize_status(cu_status_raw)

    # Escalated -> skip entirely, leave in Processing for Sam.
    # Match both the new "escalated" and legacy "needs review".
    if cu_norm in (CU_ESCALATED, CU_NEEDS_REVIEW):
        print(
            f"[NO-ACTION] ticket {ticket_id}: ClickUp '{cu_status_raw}'"
            " is escalated -- leaving for Sam"
        )
        _log_auto_handled(
            ticket_id, reply_text, cu_status_raw or cu_norm,
            "(skipped -- escalated)",
        )
        return

    zoho_status = _map_clickup_to_zoho_status(cu_norm)
    if zoho_status:
        _set_zoho_ticket_status(ticket_id, zoho_status)
        print(
            f"[NO-ACTION] ticket {ticket_id}: ClickUp '{cu_status_raw}'"
            f" -> Zoho {zoho_status}"
        )
    else:
        print(
            f"[NO-ACTION] ticket {ticket_id}: ClickUp '{cu_status_raw}'"
            " has no Zoho mapping -- left unchanged"
        )
    _log_auto_handled(
        ticket_id, reply_text, cu_status_raw or cu_norm or "(unknown)",
        zoho_status or "(unchanged)",
    )


_processing_updates: dict[str, float] = {}

def process_ticket_update(ticket_id: str) -> str | None:
    """Reprocess a ticket after a client reply. Returns agent output or None."""
    # Dedup: Zoho may fire the update webhook multiple times
    import time
    now = time.time()
    expired = [k for k, v in _processing_updates.items() if now - v > _DEDUP_TTL_SECONDS]
    for k in expired:
        del _processing_updates[k]
    if ticket_id in _processing_updates:
        print(f"Duplicate update webhook for ticket {ticket_id} -- skipping")
        return None
    _processing_updates[ticket_id] = now

    try:
        conversations_result = fetch_ticket_conversations(ticket_id)

        is_reply, latest = _is_client_reply(conversations_result, ticket_id)
        if not is_reply:
            return None

        # --- Classify the reply and sync ClickUp/Zoho ---
        from database import get_thread_by_ticket_id, update_thread

        # Read the reply text from content, falling back to summary -- the
        # conversations list endpoint omits `content` on reply threads, so
        # without the fallback every classifier below would judge empty text.
        reply_content_clean = _extract_reply_text(latest)

        # OOO / autoresponder guard -- never treat an out-of-office as a reply,
        # and undo the Processing flip Zoho's reply workflow made for it.
        if _looks_like_auto_reply(reply_content_clean):
            print(
                f"[NO-ACTION] OOO/autoresponder on ticket {ticket_id}"
                " -- not treating as a reply, restoring its status"
            )
            _restore_status_after_auto_reply_later(ticket_id, latest)
            return None

        # NO-ACTION fast path. A pure courtesy ack needs no response: realign
        # the Zoho status to mirror the linked ClickUp task and stop here --
        # never draft, never resurface, never email. This runs BEFORE the
        # resurface/draft logic below, so a courtesy reply on an
        # awaiting-client task does NOT pull it back into the queue.
        _latest_has_attach = bool(latest.get("hasAttach"))
        if not _latest_has_attach:
            try:
                _latest_has_attach = int(
                    latest.get("attachmentCount") or 0
                ) > 0
            except (ValueError, TypeError):
                _latest_has_attach = False
        if _is_no_action_reply(reply_content_clean, _latest_has_attach):
            print(
                f"[NO-ACTION] courtesy reply on ticket {ticket_id}"
                " -- realigning Zoho to ClickUp (no draft/resurface/email)"
            )
            _handle_no_action_reply(ticket_id, reply_content_clean)
            return None

        classification = _classify_client_reply(
            reply_content_clean or "(no text content)"
        )
        reply_type = classification.get("type", "substantive")
        reply_summary = classification.get("summary", "")
        print(
            f"Client reply classified as '{reply_type}'"
            f" on ticket {ticket_id}: {reply_summary}"
        )

        # Look up linked ClickUp task
        thread_info = get_thread_by_ticket_id(ticket_id)
        clickup_task_id = None
        thread_ts = None
        thread_data = {}
        was_waiting = False
        was_closed = False
        if thread_info:
            thread_ts, thread_data = thread_info
            clickup_task_id = thread_data.get("clickup_task_id")
            thread_status = thread_data.get("status", "")
            was_waiting = thread_status == THREAD_WAITING_CLIENT
            was_closed = thread_status in (
                THREAD_HANDLED, THREAD_CLOSED, THREAD_ON_PROD_SENT
            )

        # Closed-ticket replies: RE-CLOSE only on a high-confidence courtesy
        # ack. Re-closing buries the reply (Sam won't see it), so we must be
        # highly confident; on any doubt — attachments, an action signal, or
        # text we couldn't read — re-open so a human reviews it. This replaces
        # the old logic that trusted the ack/substantive classifier alone and
        # silently re-closed a "it's still broken, please re-check" reply.
        if was_closed:
            if _is_confident_ack(reply_content_clean, _latest_has_attach):
                # Genuine "thanks, all good" after resolution — re-close.
                _set_zoho_ticket_status(ticket_id, ZOHO_CLOSED)
                print(
                    f"Confident courtesy ack on closed ticket"
                    f" {ticket_id} — re-closed"
                )
                if was_waiting and thread_ts:
                    update_thread(thread_ts, status=THREAD_OPEN)
                return None
            # Not a confident ack — treat as a new/continuing issue. Re-open
            # and fall through to full reprocessing, which creates a fresh
            # ClickUp task via the normal classification + routing flow.
            print(
                f"Reply on closed ticket {ticket_id} is not a"
                " high-confidence ack — reopening to Processing"
            )
            _set_zoho_ticket_status(ticket_id, ZOHO_PROCESSING)
            if thread_ts:
                update_thread(thread_ts, status=THREAD_OPEN)

        # Acknowledgment on an open ticket — restore the working status, done.
        elif reply_type == "ack":
            _set_zoho_ticket_status(ticket_id, ZOHO_PROCESSING)
            print(
                f"Ack reply on ticket {ticket_id}"
                f" — Zoho status restored to {ZOHO_PROCESSING}"
            )
            if was_waiting and thread_ts:
                update_thread(thread_ts, status=THREAD_OPEN)
            return None

        # Substantive reply while awaiting client — re-queue + log the reply
        # as a ClickUp comment so the engineer sees what the client said.
        elif clickup_task_id and was_waiting:
            _update_clickup_task_status(
                clickup_task_id, CU_WRITE_QUEUED_UPPER
            )
            _add_clickup_comment(
                clickup_task_id,
                "Client replied: "
                + (reply_summary or reply_content_clean[:500]),
            )
            print(
                f"ClickUp task {clickup_task_id}"
                " re-queued (client replied)"
            )

        # Substantive reply on open ticket — append context
        elif clickup_task_id:
            _append_clickup_task_context(
                clickup_task_id,
                reply_summary or reply_content_clean[:500],
            )

        # Customer replied -> the ticket needs eyes again. Always put it
        # back into the active work queue (Processing), regardless of
        # assignee or prior status. (Previously called
        # _get_zoho_assignee_status, which returned the deprecated
        # "Pending Developer Fix" for engineer-assigned tickets and parked
        # the reply On Hold, so replies got lost.)
        if not was_closed:
            _set_zoho_ticket_status(ticket_id, ZOHO_PROCESSING)

        if was_waiting and thread_ts:
            update_thread(thread_ts, status=THREAD_OPEN)

        # NOTE (2026-06-18): On a client reply we deliberately DO NOT
        # auto-generate a draft reply or post the "AGENT UPDATE \u2014 CLIENT
        # REPLIED" private comment on the Zoho ticket anymore. That dumped the
        # full draft + AGENT ANALYSIS + notes onto the ticket on every reply,
        # which buried the real conversation and risked the internal analysis
        # being sent to the client. The ticket is now left for a human to
        # process. The important automated steps already ran above: the status
        # is set back to Processing and the ClickUp task is re-queued / has the
        # reply appended. A brief Slack heads-up still fires below when the
        # ticket was waiting on the client.

        # Notify in Slack if this was a waiting-client reply
        if was_waiting and thread_ts:
            try:
                from slack_sdk import WebClient
                from slack_sdk.errors import SlackApiError
                _slack_client = WebClient(
                    token=os.environ.get("SLACK_BOT_TOKEN", "")
                )
                zoho_url = (
                    "https://desk.zoho.com/support/"
                    "vomevolunteer/ShowHomePage.do"
                    f"#Cases/dv/{ticket_id}"
                )
                cu_url = (
                    f"https://app.clickup.com/t/"
                    f"{clickup_task_id}"
                ) if clickup_task_id else ""
                link_parts = [f"<{zoho_url}|Zoho>"]
                if cu_url:
                    link_parts.append(f"<{cu_url}|ClickUp>")
                links = " | ".join(link_parts)

                channel = thread_data.get("channel", "")
                if channel:
                    _slack_client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f":incoming_envelope: *Client replied*"
                            f" (was waiting on client)\n"
                            f"{reply_summary}\n\n"
                            f"{links}\n"
                            f"ClickUp task re-queued."
                            " Reply in plain English"
                            " to take action."
                        ),
                    )
                    print(
                        f"Slack notified for client reply"
                        f" on ticket {ticket_id}"
                    )
            except Exception as slack_err:
                print(
                    f"Slack notification failed for"
                    f" ticket {ticket_id}: {slack_err}"
                )

        return None
    except Exception as e:
        print(f"Agent error processing ticket update {ticket_id}: {e}")
        return None
