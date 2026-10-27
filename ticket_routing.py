"""
ticket_routing.py

After shadow triage, put every bug with whoever will fix it, without
waiting for Sam's sprint.

WHY
---
Sam, 2026-10-19: "things are happening automatically, and there's no
roadblock, but the client is not necessarily informed right away." Until
now a bug the agent could not fix sat in the sprint queue until Sam said
`route N`, so engineering only started when Sam had time to run a sprint.
The sprint is now for telling clients what is already happening: the agent
or an engineer already has the work, and Sam's reply says so.

WHAT HAPPENS, PER TRIAGED TICKET
--------------------------------
- agent_fix lane with a PR open: the agent owns it (ticket_fix). Nothing
  here, unless the client is urgent (see below): then Sam is asked to merge.
- engineer lane, or an agent_fix the agent declined to write: a Priority
  Queue task for OnlyG (backend) or Sanjay (web and mobile), with the
  agent's first pass as the engineer summary, and a private Zoho note.
  The Zoho status is NOT changed. Sam's rule (2026-10-19): New or
  Processing in Zoho means "this client is owed a reply", so nothing the
  agents do on their own moves a ticket out of it. The status changes only
  when the client has been told (`sprint_thread._after_send`). The owner
  is different: a ticket still owned by the other team from an earlier
  round moves to this route's team (Sam, 2026-10-21, #9501).
- a ticket already linked to a task (in any list), or whose incident
  matches a ticket already routed, joins that task instead: one bug, one
  task.
- Never routed here: a product decision (design_question), feature
  requests, Sam's own ops (data, billing, accounts), answers.

The sprint item stays open with its draft, the acknowledgment, so the
client hears from Sam in his next sprint. Sending it finishes the item and
moves Zoho to In Progress with the engineer's team
(sprint_thread._after_send). When the engineer marks the task on prod, the
on-prod handler emails the client and closes the ticket on its own, which
Sam approved for fixes (2026-10-19).

URGENT
------
Sam is interrupted in #support-sprints for his own rule (2026-10-19):
    a very-high account (ARR 4000+, agent.get_client_tier), or
    the same symptom from another account (the incident matcher, high
    confidence, a second account; one customer writing twice is not an
    incident).
An urgent agent fix asks Sam to merge now.

The engineer is pinged on a narrower rule (Sam, 2026-10-27: 17 "URGENT"
posts in eight days, because most enterprise accounts clear 4000 ARR). The
task's priority follows eng_priority's ladder: the same problem from two or
more accounts is URGENT and pinged once; a very-high or important account
alone is HIGH; anything else NORMAL. A ticket joining a task raises the
task's priority when it outranks it, and pings only when that makes the
task urgent. Everything below urgent is ClickUp only and reaches the
engineer in the morning digest.

SWITCH
------
    AUTO_ROUTE_ENABLED   default on. "false" leaves routing to Sam.
"""

from __future__ import annotations

import os
import re
import time

import sprint_store

# agent.get_client_tier: very-high starts at 4000.
VERY_HIGH_ARR = 4000.0

# Repos whose code Sanjay owns; everything else is OnlyG's.
FRONTEND_REPOS = {"web", "mobile"}

SHIPPED = frozenset({"on prod", "closed", "done", "complete"})

# A turned-down task is Closed with Resolution "Declined" (Sam removed the
# `declined` column, 2026-10-22). Closed alone would read as shipped; this
# keeps "the engineer says it is not a bug" apart from "it shipped".
FIELD_RESOLUTION = "63ef3458-cfa6-4a0b-ae44-18858cd555f0"
RESOLUTION_DECLINED = "8600a963-ab55-430f-a86f-2b1d0f911156"

FOOTER = ("_Filed automatically by support triage. The client has not been "
          "told yet; Sam replies from the support sprint._")

# Priority Queue fields a hand-filed task carries (System Configuration).
# #9596 (2026-10-20) was filed with only the Zoho link and Source, and no
# priority for an important client.
FIELD_TYPE = "e0e439f5-397d-432d-addd-e90fbf50cd30"
TYPE_BUG = "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df"
FIELD_PLATFORM = "5f1ff65b-18fc-49db-89aa-2c1f355ec1e7"
PLATFORMS = {"web": "946c8214-6a65-4e63-a437-d98415dc1439",
             "mobile": "070470c3-c248-4d64-8ceb-8c95df82506b"}
FIELD_COMBINED_ARR = "29c41859-f24b-4143-9af4-a34202205641"

TITLE_LENGTH = 90


def enabled() -> bool:
    return os.environ.get("AUTO_ROUTE_ENABLED", "true").strip().lower() \
        not in ("false", "0", "off", "no")


def engineer_for(card: dict) -> str:
    """OnlyG for the backend, Sanjay for web and mobile."""
    code = card.get("code_check") if isinstance(
        card.get("code_check"), dict) else {}
    repo = str(code.get("where") or "").split(":")[0].strip().lower()
    if repo in FRONTEND_REPOS:
        return "sanjay"
    if card.get("escalation_reason") == "mobile_unreadable":
        return "sanjay"
    return "onlyg"


def should_route(card: dict, fix: dict) -> tuple[bool, str]:
    """Whether this card goes to an engineer now, and the reason recorded
    with it (it feeds "what still escalates, and why")."""
    lane = card.get("lane") or ""
    code = card.get("code_check") if isinstance(
        card.get("code_check"), dict) else {}
    if code.get("verdict") == "design_question":
        return False, "design question: Sam decides"
    # #9547 asked whether the waitlist can skip a conflicting user. The code
    # check said it works as built and the card still went to OnlyG as a
    # data lookup (2026-10-20). Nothing is broken, so it is Sam's answer or
    # his feature call; he can still route it by hand.
    if code.get("verdict") == "works_as_designed":
        return False, "works as designed: Sam decides"
    if lane == "engineer":
        reason = card.get("escalation_reason") or ""
        if reason == "product_decision":
            return False, "product decision: Sam decides"
        return True, reason or "engineer_lane"
    if lane == "agent_fix":
        if fix.get("pr_url"):
            return False, "the agent opened a fix"
        return True, "agent_declined"
    return False, f"lane {lane or 'unset'}"


def _other_accounts(incident: dict, account: str) -> list[dict]:
    """Items for the incident's other tickets that belong to someone else."""
    if not incident:
        return []
    mine = _account_name(account)
    return [i for i in sprint_store.by_ticket_numbers(
                incident.get("same_as") or [])
            if _account_name(i.get("account") or "") != mine]


def _account_name(account: str) -> str:
    """"Sarasota County Government (Ultimate, 7000 CAD)" -> the name."""
    return account.split("(")[0].strip().lower()


def urgency(arr: float, account: str, incident: dict) -> str:
    """Why this is urgent, in words, or '' when it is not."""
    others = {_account_name(i.get("account") or "")
              for i in _other_accounts(incident, account)}
    if others:
        return f"same problem reported by {len(others) + 1} accounts"
    if arr >= VERY_HIGH_ARR:
        return f"very-high account ({int(arr)} CAD ARR)"
    return ""


def priority_for(item: dict, arr: float, incident: dict) -> int:
    """The engineer task's priority on eng_priority's ladder."""
    import eng_priority

    if _other_accounts(incident, item.get("account") or ""):
        return eng_priority.URGENT
    if arr >= VERY_HIGH_ARR or item.get("important"):
        return eng_priority.HIGH
    return eng_priority.NORMAL


def arr_of(account: str) -> float:
    """ARR from the account label sprint.build_context writes:
    "Sarasota County (very-high, 7000 CAD combined ARR)"."""
    found = re.search(r"([\d][\d,]*(?:\.\d+)?)\s*CAD", account or "")
    return float(found.group(1).replace(",", "")) if found else 0.0


def incident_of(item: dict) -> dict:
    """The incident as urgency() needs it: the tickets the matcher named
    for this one, as triage stored them on the card.

    Not everyone who shares the key. A key is a Sentry issue more often
    than not, and tickets tied to one noisy error are not the same problem
    as each other: #9550 (an email change) read "same problem reported by
    7 accounts" because nine unrelated tickets had been tied to one
    27k-event Sentry error (2026-10-20)."""
    key = item.get("incident_key") or ""
    card = item.get("card") or {}
    if "incident" in card:
        stored = card.get("incident") or {}
        return {"key": key, "same_as": list(stored.get("same_as") or [])}
    # Triaged before the card kept the match: a ticket key still names
    # tickets matched to each other, a Sentry key names nobody.
    if not key or key.startswith("sentry:"):
        return {"key": key, "same_as": []} if key else {}
    others = [i.get("ticket_number") for i in sprint_store.by_incident(key)
              if i.get("ticket_number") != item.get("ticket_number")]
    return {"key": key, "same_as": others}


def _existing_task(item: dict, incident: dict) -> tuple[dict, str]:
    """A task this ticket should join, and how it was found.

    Raises when ClickUp cannot be searched: filing blind is how duplicates
    are made, and the ticket still reaches Sam's sprint either way.
    """
    for other in sprint_store.by_ticket_numbers(
            (incident or {}).get("same_as") or []):
        filed = other.get("eng_task") or {}
        if not filed.get("url"):
            continue
        # #9462, #9563 and #9583 joined 868mka0f8 after it was closed
        # (2026-10-20): a sibling's task is checked like a linked one.
        live = _read_state(filed["id"])
        if live.get("status") == "unreadable":
            raise RuntimeError(f"could not read task {filed['id']}")
        if live.get("shipped"):
            continue
        return ({**filed, "priority": live.get("priority")},
                f"same problem as #{other.get('ticket_number')}")
    import clickup_search

    for task in clickup_search.linked_tasks(
            str(item.get("ticket_number") or ""),
            str(item.get("ticket_id") or "")):
        if task.get("list") != "Priority Queue":
            continue
        if (task.get("status") or "").lower() in SHIPPED:
            continue
        # The list search is cached for minutes. #9526's task was closed 70
        # seconds before triage joined it (2026-10-20), so ask ClickUp now.
        live = _read_state(task["id"])
        if live.get("status") == "unreadable":
            raise RuntimeError(f"could not read task {task['id']}")
        if live.get("shipped"):
            continue
        return ({"id": task["id"],
                 "url": task.get("url")
                 or f"https://app.clickup.com/t/{task['id']}",
                 "priority": live.get("priority")},
                "already linked to this ticket")
    return {}, ""


def _title(text: str) -> str:
    """At most TITLE_LENGTH characters, cut between words (#9596's title
    ended "every other profile opens, an")."""
    text = " ".join((text or "").split())
    if len(text) <= TITLE_LENGTH:
        return text
    cut = text[:TITLE_LENGTH].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return cut + "..."


def _fields(item: dict, card: dict, arr: float) -> list[dict]:
    """Type, Platform and Combined ARR when the card settles them. Module
    is left to the engineer: the card has no reliable module."""
    code = card.get("code_check") if isinstance(
        card.get("code_check"), dict) else {}
    repo = str(code.get("where") or "").split(":")[0].strip().lower()
    fields = []
    if (item.get("category") or card.get("category")) == "clear_bug":
        fields.append({"id": FIELD_TYPE, "value": TYPE_BUG})
    if repo in PLATFORMS:
        fields.append({"id": FIELD_PLATFORM, "value": PLATFORMS[repo]})
    if arr > 0:
        fields.append({"id": FIELD_COMBINED_ARR, "value": arr})
    return fields


def _comment(task_id: str, text: str) -> None:
    import httpx

    resp = httpx.post(
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json={"comment_text": text, "notify_all": False}, timeout=30,
    )
    resp.raise_for_status()


def _set_priority(task_id: str, priority: int) -> None:
    import httpx

    resp = httpx.put(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json={"priority": priority}, timeout=30,
    )
    resp.raise_for_status()


def route(item: dict, card: dict, fix: dict, incident: dict,
          arr: float, owner: str = "") -> dict:
    """File or join the engineer's task for a triaged item. Never raises.

    `owner` is Sam routing by hand (`route N to ...`, route_ticket): his
    choice of engineer, past the lane check and the on/off switch, through
    the same rules otherwise (Zoho status untouched, Slack only if urgent).

    {"routed": bool, "why": str, "task": {...}, "urgent": str}
    """
    if not owner and not enabled():
        return {"routed": False, "why": "auto-routing is off"}
    if (item.get("eng_task") or {}).get("url"):
        return {"routed": False, "why": "already with an engineer",
                "task": item["eng_task"]}
    if owner:
        go = True
        reason = card.get("escalation_reason") or "sam_call"
    else:
        go, reason = should_route(card, fix)
    if not go:
        return {"routed": False, "why": reason}
    try:
        return _route(item, card, incident, arr, reason, owner)
    except Exception as exc:
        print(f"[ROUTE] #{item.get('ticket_number')} not routed: {exc}")
        return {"routed": False, "why": f"routing failed ({exc})"}


def _route(item: dict, card: dict, incident: dict, arr: float,
           reason: str, forced: str = "") -> dict:
    import eng_priority
    import sprint_thread
    import zoho_actions

    number = item.get("ticket_number")
    account = item.get("account") or ""
    urgent = urgency(arr, account, incident)
    priority = priority_for(item, arr, incident)
    zoho_url = zoho_actions.ticket_url(item.get("ticket_id"))
    summary = _title(card.get("live_question") or item.get("summary") or "")

    task, joined = _existing_task(item, incident)
    owner = forced or task.get("owner") or engineer_for(card)
    clickup_id, slack_id, team, channel = sprint_thread.ENGINEERS[owner]
    if task:
        _comment(task["id"],
                 f"Also reported on #{number} ({account}): {summary}\n"
                 f"Zoho: {zoho_url}"
                 + (f"\nWhy it matters: {urgent}" if urgent else ""))
        # One bug, one ping: a join pings only when it makes the task
        # urgent (868mka0f8 was pinged four times on 2026-10-20).
        raised = eng_priority.higher(priority, task.get("priority"))
        if raised:
            try:
                _set_priority(task["id"], priority)
            except Exception as exc:
                print(f"[ROUTE] #{number} priority not raised: {exc}")
        ping = raised and priority == eng_priority.URGENT
        how = f"joined ({joined})"
    else:
        body = {
            "name": f"[#{number}] {account.split('(')[0].strip()} -- "
                    f"{summary}",
            "markdown_description": sprint_thread._task_description(
                item, footer=FOOTER),
            "assignees": [clickup_id],
            "status": "queued",
            "custom_fields": [
                {"id": sprint_thread.FIELD_ZOHO_TICKET_LINK,
                 "value": zoho_url},
                {"id": sprint_thread.FIELD_SOURCE,
                 "value": sprint_thread.SOURCE_ZOHO_TICKET},
            ] + _fields(item, card, arr),
            # #9596 (2026-10-20) and 29 of OnlyG's tasks on 2026-10-27 had
            # no priority; every routed task now carries the ladder's.
            "priority": priority,
        }
        created = sprint_thread._clickup_create(sprint_thread.PRIORITY_QUEUE,
                                                body)
        task = {"id": created.get("id"),
                "url": created.get("url")
                or f"https://app.clickup.com/t/{created.get('id')}"}
        ping = priority == eng_priority.URGENT
        how = "created"

    filed = {"id": task["id"], "url": task["url"], "owner": owner,
             "urgent": bool(urgent), "priority": priority, "how": how,
             "reason": reason}
    sprint_store.set_eng_task(item["id"], filed)
    item["eng_task"] = filed

    # A private note only. The status stays New or Processing until Sam's
    # acknowledgment goes out: that status is how anyone looking at Zoho
    # knows the client is still owed a reply.
    try:
        from zoho_desk_api import add_ticket_comment

        add_ticket_comment(item["ticket_id"],
                           f"Routed to {owner} automatically: {task['url']}")
    except Exception:
        pass
    # The owner, though, follows the route when it still names the other
    # team from an earlier round (#9501).
    try:
        if zoho_actions.follow_route(item["ticket_id"], team):
            print(f"[ROUTE] #{number} Zoho owner moved to the {team} team")
    except Exception as exc:
        print(f"[ROUTE] #{number} Zoho owner not moved: {exc}")

    if ping:
        eng_priority.ping(
            owner, task["id"],
            f"URGENT {team} ticket ({urgent}): {summary}\n"
            f"ClickUp: {task['url']} · Zoho: {zoho_url}",
            eng_priority.URGENT)
    if urgent:
        sprint_thread.alert_sam(
            f"urgent:{task['id']}",
            f"*Urgent, with {owner}:* #{number} {account}: {summary}\n"
            f"Why urgent: {urgent}. ClickUp: {task['url']}\n"
            "Nothing was sent to the client. The acknowledgment is ready "
            "in your sprint.")
    print(f"[ROUTE] #{number} {how} {task['url']} for {owner} ({reason}), "
          f"priority {eng_priority.name(priority)}"
          + (", pinged" if ping else ""))
    return {"routed": True, "why": reason, "task": filed, "urgent": urgent,
            "priority": priority, "pinged": ping}


def flag_urgent_fix(item: dict, card: dict, pr_url: str, incident: dict,
                    arr: float) -> bool:
    """An agent fix for an urgent client: the only wait is Sam's merge."""
    urgent = urgency(arr, item.get("account") or "", incident)
    if not urgent or not pr_url:
        return False
    code = card.get("code_check") if isinstance(
        card.get("code_check"), dict) else {}
    repo = str(code.get("where") or "").split(":")[0].strip().lower()
    production = ("then you deploy it" if repo in FRONTEND_REPOS
                  else "then OnlyG takes it to production")
    summary = _title(card.get("live_question") or item.get("summary") or "")
    import sprint_thread

    return sprint_thread.alert_sam(
        f"urgent-fix:{pr_url}",
        f"*Urgent fix waiting on your merge:* #{item.get('ticket_number')} "
        f"{item.get('account') or ''}: {summary}\nWhy urgent: {urgent}. "
        f"PR: {pr_url}\nMerge into the dev branch, {production}. Nothing "
        "was sent to the client; the acknowledgment is ready in your sprint.")


_STATE_TTL = 60.0
_states: dict[str, tuple[float, dict]] = {}


def task_state(filed: dict) -> dict:
    """The engineer task's live status: {"status", "shipped", "declined",
    "assignee"}.
    {"status": "unreadable"} when ClickUp cannot be read. Cached a minute,
    because one sprint_queue call asks twice per routed item (finish the
    shipped ones, then show the rest)."""
    if not (filed or {}).get("id"):
        return {}
    now = time.monotonic()
    hit = _states.get(filed["id"])
    if hit and now - hit[0] < _STATE_TTL:
        return hit[1]
    state = _read_state(filed["id"])
    if state.get("status") != "unreadable":
        _states[filed["id"]] = (now, state)
    return state


def _read_state(task_id: str) -> dict:
    import httpx

    filed = {"id": task_id}
    try:
        resp = httpx.get(
            f"https://api.clickup.com/api/v2/task/{filed['id']}",
            headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN",
                                                     "")},
            timeout=15,
        )
        resp.raise_for_status()
        task = resp.json()
    except Exception as exc:
        return {"status": "unreadable", "shipped": False, "error": str(exc)}
    status = ((task.get("status") or {}).get("status") or "").lower()
    names = [a.get("username") or a.get("email") or ""
             for a in task.get("assignees") or []]
    # "declined" itself still counts until the old column's tasks are moved.
    declined = status == "declined" or _resolution(task) == RESOLUTION_DECLINED
    import eng_priority

    return {"status": status, "shipped": status in SHIPPED and not declined,
            "declined": declined, "priority": eng_priority.of_task(task),
            "assignee": ", ".join(n for n in names if n),
            # Where the task sits now, for the sprint card's links line.
            "name": task.get("name") or "",
            "list": (task.get("list") or {}).get("name") or "",
            "url": task.get("url") or ""}


def _resolution(task: dict) -> str:
    """The Resolution option id. ClickUp returns a dropdown value as the
    option's orderindex, so it is matched back to an id."""
    for field in task.get("custom_fields") or []:
        if field.get("id") != FIELD_RESOLUTION:
            continue
        value = field.get("value")
        if value is None or value == "":
            return ""
        options = (field.get("type_config") or {}).get("options") or []
        for opt in options:
            if str(value) in (str(opt.get("id")), str(opt.get("orderindex"))):
                return str(opt.get("id") or "")
        return str(value)
    return ""
