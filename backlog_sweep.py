"""
backlog_sweep.py

Triage the work that reached the engineers before triage existed, and take
off their plates what an answer or an agent fix can settle.

    py backlog_sweep.py inventory              list the backlog, read only
    railway run --service vome-os-worker py backlog_sweep.py triage --limit 10
    py backlog_sweep.py sheet                  the review sheet, by group
    railway run --service vome-os-worker py backlog_sweep.py audit
                                               Zoho and ClickUp in sync?
    railway run --service vome-os-worker py backlog_sweep.py apply answer \\
        868abc123 zoho:569440000043050400 --confirm

WHY
---
Sam, 2026-10-20: shadow triage only runs when a Zoho webhook fires (a new
ticket or a customer reply), and the sprint queue only holds New and
Processing. Everything already In Progress with OnlyG or Sanjay, or already
queued in the Priority Queue, was filed before the triage director existed
and never got a card. Some of it is user education or a bug an agent can
fix, sitting in an engineer's queue.

THE FOUR STEPS
--------------
1. inventory  Priority Queue tasks assigned to OnlyG or Sanjay at `queued`
              or `in progress`, plus Zoho tickets In Progress with the
              backend or frontend team that no such task carries. Reads only.
2. triage     The triage director on each, with the engineer task's own
              description and comments as existing work. It needs the
              worker's code tokens for the code check (on the 2026-10-16
              pilot the code check overturned 2 of 8 calls), so it runs
              under `railway run --service vome-os-worker`. Sends nothing
              and changes nothing; only the agent trace is written.
              Resumable: a carded item is skipped unless --refresh.
3. sheet      One page grouped by what would happen: answer, agent fix,
              close, Sam decides, Sam's ops, keep with the engineer, needs
              a look. Sam approves a group or picks items from it.
4. apply      Only the items Sam named, only with --confirm, only under
              the worker's environment:
                answer  the card and a drafted reply join the sprint as an
                        item (Sam sends it as usual), and the engineer task
                        is released.
                fix     ticket_fix opens the PR and files Agent Fixes, then
                        the engineer task is released. A declined fix leaves
                        the task with the engineer and comments why.
                close   nothing is owed: the engineer task is released and
                        the Zoho ticket closes with no closure email.
                note    the first pass is commented on the engineer task,
                        which stays theirs.

"Released" means a comment saying who took it and why, the engineer
removed as assignee, and the task Closed. Never `user education`, `needs
client info` or `on prod`: each of those emails the client on its own
(main.py, /webhook/clickup-status).

Nothing here emails a client. A reply goes out only when Sam sends the
sprint item, exactly as for a new ticket.

Output lives in backlog_sweep_out/ (gitignored: it holds customer names and
threads).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()


def _use_public_database() -> None:
    """`railway run --service vome-os-worker` hands over the worker's
    private database address (postgres.railway.internal), which resolves
    only inside Railway. The local .env holds the public address of the
    same database. Must run before anything imports vomeos.config."""
    url = os.environ.get("DATABASE_URL", "")
    if ".railway.internal" not in url:
        return
    # The Postgres service's own public address wins: the .env copy goes
    # stale when the password rotates (2026-10-24).
    if os.environ.get("DATABASE_PUBLIC_URL"):
        os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]
        return
    from dotenv import dotenv_values

    public = dotenv_values(Path(__file__).resolve().parent / ".env"
                           ).get("DATABASE_URL") or ""
    if public and ".railway.internal" not in public:
        os.environ["DATABASE_URL"] = public


_use_public_database()

OUT_DIR = Path(os.environ.get("BACKLOG_SWEEP_DIR")
               or Path(__file__).resolve().parent / "backlog_sweep_out")
INVENTORY = "inventory.json"
CARDS = "cards.json"
APPLIED = "applied.json"
SHEET = "sheet.html"

CLICKUP_BASE = "https://api.clickup.com/api/v2"
PRIORITY_QUEUE = "901113386257"

# On the engineers' plate (status_constants.BUCKET_ACTIVE without `on dev`,
# which is finished work waiting on a release). `in progress` is triaged
# too, but flagged as started: an engineer may be mid-fix, so it is never
# applied without --started.
SWEEP_STATUSES = ("queued", "in progress")
STARTED = "in progress"

ENGINEERS = {49257687: "onlyg", 4434086: "sanjay"}
ZOHO_TEAMS = {
    os.environ.get("ZOHO_AGENT_BACKEND", "569440000023160001"): "onlyg",
    os.environ.get("ZOHO_AGENT_FRONTEND", "569440000023159001"): "sanjay",
}
ZOHO_DONE = ("Closed", "Resolved")

# Zoho pages at 100, newest activity first. Twenty pages reaches back well
# past the oldest In Progress ticket on 2026-10-20.
ZOHO_PAGES = 20

_TICKET_IN_NAME = re.compile(r"#(\d{3,6})\b")

GROUPS = (
    ("answer", "Answer it (user education)",
     "A reply settles it. The card and a draft join your sprint; the "
     "engineer task is released."),
    ("agent_fix", "Agent fix",
     "The code check found a small bug. The agent opens the PR and files "
     "Agent Fixes; the engineer task is released."),
    ("close", "Close, nothing owed",
     "Already settled, or the customer went quiet on a question we asked."),
    ("sam_decides", "Sam decides (feature or product call)",
     "Works as built, or they want something Vome does not do."),
    ("sam_ops", "Sam's ops (data, billing, accounts)",
     "Account lookups, data fixes and access, done by Sam in Zoho."),
    ("planned", "Planned build work",
     "Feature work handed to an engineer with no client ticket. Left "
     "alone."),
    ("engineer", "Keep with the engineer",
     "Real engineering work. Optionally comment the first pass on the task."),
    ("needs_look", "Needs a look",
     "The card could not settle it confidently."),
    ("unreadable", "Not triaged",
     "Triage failed or the ticket could not be read."),
)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _path(name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / name


def _load(name: str, default):
    path = _path(name)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save(name: str, data) -> None:
    # Written whole each time so an interrupted run never leaves half a file.
    path = _path(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8", newline="\n")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 1. Inventory
# ---------------------------------------------------------------------------

def _clickup_headers() -> dict:
    token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not token:
        raise RuntimeError("no CLICKUP_API_TOKEN")
    return {"Authorization": token, "Content-Type": "application/json"}


def _priority_queue() -> list[dict]:
    """Every open Priority Queue task. Raises on a failed page: a partial
    list would read as a smaller backlog than the real one."""
    tasks: list[dict] = []
    for page in range(30):
        resp = httpx.get(
            f"{CLICKUP_BASE}/list/{PRIORITY_QUEUE}/task",
            headers=_clickup_headers(),
            params={"page": page, "include_closed": "false",
                    "subtasks": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        batch = body.get("tasks") or []
        tasks.extend(batch)
        if body.get("last_page") or len(batch) < 100:
            break
    return tasks


def task_entry(task: dict) -> dict | None:
    """The sweep's record for one Priority Queue task, or None when it is
    not on an engineer's plate."""
    from zoho_links import extract_zoho_ticket_id

    status = ((task.get("status") or {}).get("status") or "").lower()
    if status not in SWEEP_STATUSES:
        return None
    owners = [ENGINEERS[a.get("id")] for a in task.get("assignees") or []
              if a.get("id") in ENGINEERS]
    if not owners:
        return None
    name = task.get("name") or ""
    number = _TICKET_IN_NAME.search(name)
    return {
        "key": str(task.get("id")),
        "source": "clickup",
        "task_id": str(task.get("id")),
        "task_url": task.get("url") or
        f"https://app.clickup.com/t/{task.get('id')}",
        "name": name,
        "status": status,
        "started": status == STARTED,
        "engineers": owners,
        "created": _ms_to_date(task.get("date_created")),
        "updated": _ms_to_date(task.get("date_updated")),
        "priority": ((task.get("priority") or {}).get("priority") or ""),
        "zoho_ticket_id": extract_zoho_ticket_id(task) or "",
        "ticket_number": number.group(1) if number else "",
        "zoho_status": "",
    }


def _ms_to_date(value) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc
                                      ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def zoho_entry(row: dict) -> dict | None:
    """A Zoho ticket In Progress with an engineering team, or None."""
    if row.get("status") != "In Progress":
        return None
    owner = ZOHO_TEAMS.get(str(row.get("assigneeId") or ""))
    if not owner:
        return None
    ticket_id = str(row.get("id") or "")
    return {
        "key": f"zoho:{ticket_id}",
        "source": "zoho",
        "task_id": "",
        "task_url": "",
        "name": row.get("subject") or "",
        "status": "zoho in progress",
        "started": False,
        "engineers": [owner],
        "created": str(row.get("createdTime") or "")[:10],
        "updated": str(row.get("customerResponseTime")
                       or row.get("modifiedTime") or "")[:10],
        "priority": "",
        "zoho_ticket_id": ticket_id,
        "ticket_number": str(row.get("ticketNumber") or ""),
        "zoho_status": "In Progress",
    }


def _zoho_in_progress(pages: int) -> list[dict]:
    import sprint

    rows: list[dict] = []
    for page in range(pages):
        batch = sprint._pull_page(offset=page * sprint.PAGE_SIZE)
        rows.extend(batch)
        if len(batch) < sprint.PAGE_SIZE:
            break
    return rows


def _already_triaged(ticket_id: str) -> dict:
    """The sprint item triage already wrote for this ticket, if any. A read
    only, and a failed read is treated as "not triaged" (the sweep then
    triages it again, which costs a run, not a mistake)."""
    if not ticket_id:
        return {}
    try:
        import sprint_store

        if not sprint_store.ready():
            return {}
        item = sprint_store.by_ticket(ticket_id) or {}
    except Exception:
        return {}
    return item if item.get("card") else {}


def build_inventory(zoho_pages: int = ZOHO_PAGES,
                    include_triaged: bool = False) -> dict:
    entries = [e for e in map(task_entry, _priority_queue()) if e]
    linked = {e["zoho_ticket_id"] for e in entries if e["zoho_ticket_id"]}

    zoho_rows = _zoho_in_progress(zoho_pages)
    statuses = {str(r.get("id")): r.get("status") or "" for r in zoho_rows}
    for entry in entries:
        entry["zoho_status"] = statuses.get(entry["zoho_ticket_id"], "")
    for row in zoho_rows:
        entry = zoho_entry(row)
        if entry and entry["zoho_ticket_id"] not in linked:
            entries.append(entry)

    skipped = []
    if not include_triaged:
        kept = []
        for entry in entries:
            item = _already_triaged(entry["zoho_ticket_id"])
            if item:
                skipped.append({"key": entry["key"],
                                "ticket_number": entry["ticket_number"],
                                "sprint_item": item.get("number")})
            else:
                kept.append(entry)
        entries = kept

    entries.sort(key=lambda e: (e["updated"] or e["created"] or "9999"))
    return {"generated_at": _now(), "entries": entries,
            "already_triaged": skipped}


def summarise_inventory(inv: dict) -> str:
    entries = inv.get("entries") or []
    lines = [f"{len(entries)} item(s) on the engineers' plates to sweep."]
    by_owner: dict[str, dict[str, int]] = {}
    for e in entries:
        for owner in e["engineers"]:
            row = by_owner.setdefault(owner, {})
            row[e["status"]] = row.get(e["status"], 0) + 1
    for owner, counts in sorted(by_owner.items()):
        parts = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        lines.append(f"  {owner}: {parts}")
    no_ticket = sum(1 for e in entries if not e["zoho_ticket_id"])
    closed = sum(1 for e in entries if e["zoho_status"] in ZOHO_DONE)
    if no_ticket:
        lines.append(f"  {no_ticket} task(s) with no Zoho ticket linked "
                     "(triaged from the task itself)")
    if closed:
        lines.append(f"  {closed} task(s) whose Zoho ticket is already "
                     "closed")
    skipped = inv.get("already_triaged") or []
    if skipped:
        lines.append(f"  {len(skipped)} skipped: already carded by shadow "
                     "triage (--include-triaged to sweep them too)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Triage
# ---------------------------------------------------------------------------

def _task_text(task_id: str, limit: int = 6000) -> str:
    """The engineer task's description and comments, newest comment last."""
    if not task_id:
        return ""
    from clickup_tasks import extract_comment_text

    headers = _clickup_headers()
    task = httpx.get(f"{CLICKUP_BASE}/task/{task_id}", headers=headers,
                     params={"include_markdown_description": "true"},
                     timeout=30).json()
    comments = httpx.get(f"{CLICKUP_BASE}/task/{task_id}/comment",
                         headers=headers, timeout=30
                         ).json().get("comments") or []
    parts = [task.get("markdown_description") or task.get("description")
             or ""]
    # ClickUp returns comments newest first.
    for comment in reversed(comments):
        who = ((comment.get("user") or {}).get("username") or "someone")
        when = _ms_to_date(comment.get("date"))
        text = extract_comment_text(comment)
        if text:
            parts.append(f"[{when} {who}] {text}")
    return "\n\n".join(p for p in parts if p)[:limit]


def engineer_task_note(entry: dict, text: str) -> str:
    """The engineer task, as existing work the director weighs."""
    if not entry.get("task_id"):
        owner = ", ".join(entry["engineers"])
        return (f"ENGINEER: Zoho has this ticket In Progress with {owner}; "
                "no Priority Queue task is linked to it.")
    return (
        f"ENGINEER TASK: {entry['task_id']} \"{entry['name']}\", status "
        f"{entry['status']} (not shipped), assigned to "
        f"{', '.join(entry['engineers'])}, opened {entry['created']}, last "
        f"updated {entry['updated']}.\nIts description and comments:\n"
        f"{text or '(empty)'}"
    )


def build_context(entry: dict) -> tuple[dict, dict]:
    """(context for the director, facts kept beside the card)."""
    import sprint

    text = _task_text(entry.get("task_id", ""))
    note = engineer_task_note(entry, text)

    if entry.get("zoho_ticket_id"):
        import shadow_triage

        context = sprint.build_context({"id": entry["zoho_ticket_id"],
                                        "ticketNumber":
                                        entry.get("ticket_number") or ""})
        if not context:
            raise RuntimeError("the Zoho ticket could not be read")
        number = context.get("ticket_number") or entry.get("ticket_number")
        release = shadow_triage.release_state(number,
                                              entry["zoho_ticket_id"])
        extra = [note]
        if release.get("task_id"):
            extra.append(f"RELEASE STATE: {release['text']}")
        context["existing_tasks"] = "\n\n".join(
            [context.get("existing_tasks") or ""] + extra).strip()
        facts = {
            "ticket_number": number,
            "subject": context.pop("_subject", ""),
            "arr": context.pop("_arr", 0),
            "zoho_status": context.pop("_status", ""),
            "account": context.get("account", ""),
            "release_live": bool(release.get("live")),
            "release_task": release.get("task_id") or "",
        }
        context.pop("_ticket_id", None)
        facts["images"] = context.pop("_images", [])
        return context, facts

    # A task nobody linked to a ticket (Sentry, field feedback, internal).
    # The task is the whole report, so it stands in for the thread.
    context = {
        "ticket_number": f"task {entry['task_id']}",
        "account": "no client ticket linked",
        "thread": f"{entry['name']}\n\n{text or '(no description)'}",
        "attachments": "none",
        "existing_tasks": ("This is the engineer task itself; no Zoho ticket "
                           "is linked, so no client is waiting on a reply."
                           f"\n\n{engineer_task_note(entry, '(above)')}"),
        "kb_matches": sprint.find_kb_matches(entry["name"], text[:1500]),
    }
    facts = {"ticket_number": "", "subject": entry["name"], "arr": 0,
             "zoho_status": "", "account": "", "release_live": False,
             "release_task": "", "images": []}
    return context, facts


def triage_one(entry: dict) -> dict:
    from vomeos import run_agent
    import sprint

    # Retried: Zoho now and then returns a corrupt compressed body ("Error -3
    # while decompressing data") that reads cleanly a few seconds later,
    # the same quirk sprint._pull_page retries for.
    for pause in (0, 3, 8):
        if pause:
            time.sleep(pause)
        try:
            context, facts = build_context(entry)
            break
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
    else:
        return {"key": entry["key"], "status": "unreadable", "why": failure,
                "card": {}, "triaged_at": _now()}
    images = facts.pop("images", [])
    result = run_agent(sprint.AGENT, context=context,
                       subject_type="backlog_sweep", subject_id=entry["key"],
                       images=images)
    card = result.data or {}
    # Every engineer item is exactly what the deep pass is for (lane
    # engineer or sam_ops, or unsure). Without it the sweep's cards were
    # the shallow kind the 2026-10-24 cleanup set out to replace.
    if result.ok:
        import deep_triage

        card = deep_triage.deepen(
            context, card, images,
            ticket_id=entry.get("zoho_ticket_id") or entry["key"],
            ticket_number=facts.get("ticket_number") or "",
            arr=float(facts.get("arr") or 0), first_result=result,
            source="backlog_sweep")
    record = {"key": entry["key"], "triaged_at": _now(), **facts,
              "status": result.status, "card": card}
    if not result.ok:
        record["why"] = result.why()
    return record


def run_triage(limit: int, only: list[str], refresh: bool) -> int:
    inv = _load(INVENTORY, None)
    if not inv:
        print("No inventory yet. Run `py backlog_sweep.py inventory` first.")
        return 1
    if not any(os.environ.get(v) for v in ("VOMEOS_GITHUB_TOKEN_VOMEADMIN",
                                           "VOMEOS_BITBUCKET_TOKEN")):
        print("No code tokens in this environment, so the code check cannot "
              "run and the cards would be the text-only kind the pilot "
              "showed are not enough.\nRun it as:\n  railway run --service "
              "vome-os-worker py backlog_sweep.py triage ...")
        return 1

    cards = _load(CARDS, {})
    todo = [e for e in inv["entries"]
            if (not only or e["key"] in only
                or e.get("ticket_number") in only)
            and (refresh or e["key"] not in cards
                 or cards[e["key"]].get("status") != "ok")]
    todo = todo[:limit] if limit else todo
    print(f"Triaging {len(todo)} item(s). Nothing is sent or changed.")
    for index, entry in enumerate(todo, start=1):
        label = (f"#{entry['ticket_number']}" if entry["ticket_number"]
                 else entry["key"])
        print(f"[{index}/{len(todo)}] {label} {entry['name'][:60]}",
              flush=True)
        record = triage_one(entry)
        cards[entry["key"]] = record
        _save(CARDS, cards)
        group, why = bucket(entry, record)
        print(f"    -> {group}: {why}")
    print(f"Cards in {_path(CARDS)}. Next: py backlog_sweep.py sheet")
    return 0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def bucket(entry: dict, record: dict) -> tuple[str, str]:
    """Which group a triaged item lands in, and the reason in a clause.

    Order matters: the first rule that applies wins. A fix proven live and
    nothing owed both beat the lane, because the work is already done; a
    product call beats an engineer lane, because nothing is broken.
    """
    card = (record or {}).get("card") or {}
    if (record or {}).get("status") != "ok" or not card:
        return "unreadable", (record or {}).get("why") or "no card"

    lane = card.get("lane") or ""
    category = card.get("category") or ""
    code = card.get("code_check") if isinstance(card.get("code_check"),
                                                dict) else {}
    verdict = code.get("verdict") or ""

    if record.get("release_live"):
        return "close", "the agent fix for it is live; the client is told " \
                        "by the release resolution"
    if lane == "housekeeping":
        return "close", "nothing is owed"
    if category == "awaiting_client":
        return "close", "we asked the client and they have not answered"
    if lane == "sam_ops" or category in ("email_update", "account_deletion"):
        return "sam_ops", "data, billing or account work"
    if (lane == "feature" or category == "feature_request"
            or verdict == "design_question"
            or card.get("escalation_reason") == "product_decision"):
        # A build task nobody linked to a ticket is the roadmap, not a
        # support request: the pilot's audit log design and announcement
        # card tasks (2026-10-21) are work Sam already handed over.
        if not entry.get("zoho_ticket_id"):
            return "planned", "planned build work, not a client ticket"
        return "sam_decides", "a feature or a product call"
    if lane == "agent_fix" or (verdict == "bug"
                               and code.get("fix_risk") == "low"
                               and lane != "engineer"):
        return "agent_fix", f"small bug at {code.get('where') or '?'}"
    if (lane == "answer" or verdict == "works_as_designed"
            or category in ("user_education", "admin_action",
                            "misdirected")):
        return "answer", "a reply settles it"
    if category == "needs_human" or card.get("confidence") == "low":
        return "needs_look", card.get("why_human") or "low confidence"
    if lane == "engineer":
        return "engineer", card.get("escalation_reason") or "engineer lane"
    return "needs_look", f"lane {lane or 'unset'}, category {category}"


def grouped(inv: dict, cards: dict) -> dict[str, list]:
    """group -> [(entry, record, why)], in inventory order."""
    out: dict[str, list] = {key: [] for key, _, _ in GROUPS}
    for entry in inv.get("entries") or []:
        record = cards.get(entry["key"])
        if not record:
            continue
        group, why = bucket(entry, record)
        out[group].append((entry, record, why))
    return out


# ---------------------------------------------------------------------------
# 3. Sheet
# ---------------------------------------------------------------------------

def _zoho_url(ticket_id: str) -> str:
    return ("https://support.vomevolunteer.com/support/vomevolunteer/"
            f"ShowHomePage.do#Cases/dv/{ticket_id}")


def render_sheet(inv: dict, cards: dict, applied: dict) -> str:
    groups = grouped(inv, cards)
    total = len(inv.get("entries") or [])
    carded = sum(len(v) for v in groups.values())
    esc = html.escape

    sections = []
    nav = []
    for key, title, blurb in GROUPS:
        rows = groups[key]
        nav.append(f'<a href="#{key}">{esc(title)} <b>{len(rows)}</b></a>')
        if not rows:
            continue
        items = []
        for entry, record, why in rows:
            card = record.get("card") or {}
            code = card.get("code_check") if isinstance(
                card.get("code_check"), dict) else {}
            number = record.get("ticket_number") or entry["ticket_number"]
            links = []
            if entry["task_url"]:
                links.append(f'<a href="{esc(entry["task_url"])}">ClickUp'
                             '</a>')
            if entry["zoho_ticket_id"]:
                zoho = esc(_zoho_url(entry["zoho_ticket_id"]))
                links.append(f'<a href="{zoho}">Zoho</a>')
            flags = []
            if entry["started"]:
                flags.append('<span class="flag warn">started: ask '
                             f'{esc(", ".join(entry["engineers"]))} first'
                             '</span>')
            if record.get("zoho_status") in ZOHO_DONE:
                flags.append('<span class="flag">Zoho ticket '
                             f'{esc(record["zoho_status"])}</span>')
            if not entry["zoho_ticket_id"]:
                flags.append('<span class="flag">no client ticket</span>')
            if card.get("important_client"):
                flags.append('<span class="flag warn">important client'
                             '</span>')
            if str(card.get("blocking_claims") or "").strip():
                flags.append('<span class="flag warn">unbacked claim in '
                             'thread</span>')
            done = applied.get(entry["key"])
            if done:
                flags.append(f'<span class="flag ok">applied: '
                             f'{esc(done.get("action", ""))}</span>')
            code_line = ""
            if code:
                code_line = (
                    f'<div class="kv"><span>Code check</span>'
                    f'{esc(str(code.get("verdict") or "?"))} '
                    f'({esc(str(code.get("confidence") or "?"))})'
                    + (f' at <code>{esc(str(code.get("where")))}</code>'
                       if code.get("where") else "")
                    + (f'. {esc(str(code.get("finding") or ""))}'
                       if code.get("finding") else "")
                    + "</div>")
            human = str(card.get("why_human") or "").strip()
            human_line = (f'<div class="kv"><span>Needs a person</span>'
                          f'{esc(human)}</div>' if human else "")

            def field(name: str, card: dict = card) -> str:
                return esc(str(card.get(name) or ""))
            items.append(f"""
<article>
  <header>
    <div class="id"><code>{esc(entry["key"])}</code>
      {f"<b>#{esc(number)}</b>" if number else ""}
      <span class="muted">{esc(record.get("account") or "")}</span></div>
    <div class="links">{" · ".join(links)}</div>
  </header>
  <h3>{esc(entry["name"][:140])}</h3>
  <div class="meta">{esc(", ".join(entry["engineers"]))}
    · {esc(entry["status"])} · opened {esc(entry["created"])}
    · updated {esc(entry["updated"])} · {field("category")}
    / {field("lane")} ({field("confidence")})</div>
  <div class="flags">{"".join(flags)}</div>
  <div class="kv"><span>Why here</span>{esc(why)}</div>
  <div class="kv"><span>Situation</span>{field("situation")}</div>
  <div class="kv"><span>Waiting on</span>{field("live_question")}</div>
  {code_line}
  <div class="kv"><span>Action</span>{field("recommended_action")}</div>
  {human_line}
</article>""")
        sections.append(
            f'<section id="{key}"><h2>{esc(title)} '
            f'<span class="count">{len(rows)}</span></h2>'
            f'<p class="blurb">{esc(blurb)}</p>{"".join(items)}</section>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Engineering Backlog Sweep</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66;
  --line:#e3e2dc; --accent:#3d5afe; --warn:#b4540a; --warnbg:#fdf0e2;
  --ok:#1f7a4d; --okbg:#e3f4ea; --flagbg:#efeee9; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#161615; --card:#1f1f1d; --ink:#ecebe6; --muted:#a3a29b;
  --line:#33322e; --accent:#8c9eff; --warn:#f0a35e; --warnbg:#3a2a1a;
  --ok:#6fd3a0; --okbg:#173326; --flagbg:#2b2a27; }} }}
:root[data-theme="dark"] {{ --bg:#161615; --card:#1f1f1d; --ink:#ecebe6;
  --muted:#a3a29b; --line:#33322e; --accent:#8c9eff; --warn:#f0a35e;
  --warnbg:#3a2a1a; --ok:#6fd3a0; --okbg:#173326; --flagbg:#2b2a27; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:26px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 16px; }}
nav {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 24px; }}
nav a {{ background:var(--card); border:1px solid var(--line);
  border-radius:999px; padding:4px 12px; color:var(--ink);
  text-decoration:none; font-size:13px; }}
h2 {{ font-size:19px; margin:32px 0 2px; }}
.count {{ color:var(--muted); font-weight:500; }}
.blurb {{ color:var(--muted); margin:0 0 12px; }}
article {{ background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:14px 16px; margin:0 0 10px; }}
article header {{ display:flex; justify-content:space-between; gap:12px;
  flex-wrap:wrap; font-size:13px; }}
article h3 {{ font-size:16px; margin:6px 0 2px; overflow-wrap:anywhere; }}
.meta,.muted {{ color:var(--muted); font-size:13px; }}
.links a {{ color:var(--accent); }}
.flags {{ margin:6px 0; display:flex; flex-wrap:wrap; gap:6px; }}
.flag {{ background:var(--flagbg); border-radius:6px; padding:1px 8px;
  font-size:12px; }}
.flag.warn {{ background:var(--warnbg); color:var(--warn); }}
.flag.ok {{ background:var(--okbg); color:var(--ok); }}
.kv {{ display:grid; grid-template-columns:120px 1fr; gap:10px;
  margin:4px 0; font-size:14px; overflow-wrap:anywhere; }}
.kv span {{ color:var(--muted); }}
code {{ font-size:12px; }}
@media (max-width:600px) {{ .kv {{ grid-template-columns:1fr; gap:0; }} }}
</style></head>
<body><main>
<h1>Engineering backlog sweep</h1>
<p class="sub">{carded} of {total} items triaged · generated {esc(_now())}
 · nothing has been sent or changed except items marked applied</p>
<nav>{"".join(nav)}</nav>
{"".join(sections)}
</main></body></html>
"""


def run_sheet() -> int:
    inv = _load(INVENTORY, None)
    cards = _load(CARDS, {})
    if not inv or not cards:
        print("Nothing to show: run inventory and triage first.")
        return 1
    page = render_sheet(inv, cards, _load(APPLIED, {}))
    path = _path(SHEET)
    path.write_text(page, encoding="utf-8", newline="\n")
    groups = grouped(inv, cards)
    for key, title, _ in GROUPS:
        print(f"  {len(groups[key]):>3}  {title}")
    print(f"Wrote {path}")
    return 0


# ---------------------------------------------------------------------------
# 4. Apply
# ---------------------------------------------------------------------------

ACTIONS = ("answer", "fix", "close", "note")

RELEASE_FOOTER = ("_Backlog sweep, approved by Sam. Reopen and reassign "
                  "yourself if this is wrong._")


def _comment(task_id: str, text: str) -> None:
    resp = httpx.post(f"{CLICKUP_BASE}/task/{task_id}/comment",
                      headers=_clickup_headers(),
                      json={"comment_text": text, "notify_all": False},
                      timeout=30)
    resp.raise_for_status()


def release_task(entry: dict, why: str) -> str:
    """Take a Priority Queue task off the engineer's plate: a comment
    saying why, the engineer unassigned, the task Closed. Closed fires no
    client email; user education, needs client info and on prod all do."""
    task_id = entry.get("task_id")
    if not task_id:
        return "no engineer task to release"
    _comment(task_id, f"{why}\n\n{RELEASE_FOOTER}")
    remove = [cid for cid, name in ENGINEERS.items()
              if name in entry["engineers"]]
    resp = httpx.put(f"{CLICKUP_BASE}/task/{task_id}",
                     headers=_clickup_headers(),
                     json={"status": "Closed",
                           "assignees": {"add": [], "rem": remove}},
                     timeout=30)
    resp.raise_for_status()
    return f"released {task_id}"


def first_pass_text(record: dict) -> str:
    card = record.get("card") or {}
    code = card.get("code_check") if isinstance(card.get("code_check"),
                                                dict) else {}
    lines = ["First pass from the backlog sweep (unverified, please "
             "confirm):",
             f"Situation: {card.get('situation') or ''}",
             f"Client is waiting on: {card.get('live_question') or ''}"]
    if code:
        lines.append(f"Code check: {code.get('verdict') or '?'} "
                     f"({code.get('confidence') or '?'})"
                     + (f" at {code['where']}" if code.get("where") else "")
                     + (f". {code['finding']}" if code.get("finding")
                        else ""))
        if code.get("fix"):
            lines.append(f"Suggested fix: {code['fix']}")
    lines.append(f"Recommended: {card.get('recommended_action') or ''}")
    return "\n".join(lines)


def _adopt(entry: dict, record: dict) -> dict:
    """Put the ticket in Sam's sprint with its card and a drafted reply.

    No eng_task on the item, on purpose: after Sam sends, an answer closes
    the Zoho ticket (sprint_thread._after_send) instead of handing it back
    to an engineer.
    """
    import shadow_triage
    import sprint
    import sprint_store
    import zoho_actions

    ticket_id = entry["zoho_ticket_id"]
    card = record.get("card") or {}
    context = sprint.build_context({"id": ticket_id,
                                    "ticketNumber": entry["ticket_number"]})
    if not context:
        raise RuntimeError("the Zoho ticket could not be read")
    rcpt = zoho_actions.recipients(ticket_id)
    number = context.get("ticket_number") or entry["ticket_number"]
    release = shadow_triage.release_state(number, ticket_id)
    draft = shadow_triage.draft_reply(
        number, card, context, rcpt, release,
        language=shadow_triage._language(context.get("thread", "")))
    try:
        arr = float(record.get("arr") or 0)
    except (TypeError, ValueError):
        arr = 0.0
    item = sprint_store.upsert_ticket(
        ticket_id=ticket_id, ticket_number=number,
        category=str(card.get("category") or ""),
        account=record.get("account") or context.get("account", ""),
        subject=record.get("subject") or entry["name"],
        summary=str(card.get("live_question") or entry["name"])[:300],
        important=bool(card.get("important_client")) or arr > 1500,
        card={**card, "backlog_sweep": entry["key"]},
        draft=draft["text"], draft_status=draft["status"],
        draft_why=draft["why"], recipients=rcpt,
        last_thread_id=rcpt.get("thread_id") or "",
    )
    if not item:
        raise RuntimeError("the sprint item could not be recorded")
    return {"item": item.get("number"), "draft": draft["status"]}


def apply_one(action: str, entry: dict, record: dict) -> dict:
    card = record.get("card") or {}
    if action == "note":
        if not entry.get("task_id"):
            return {"ok": False, "why": "no engineer task to comment on"}
        _comment(entry["task_id"], first_pass_text(record))
        return {"ok": True, "did": "first pass commented"}

    if action == "answer":
        if not entry.get("zoho_ticket_id"):
            return {"ok": False, "why": "no client ticket to answer"}
        adopted = _adopt(entry, record)
        released = release_task(
            entry, f"A reply settles this (not an engineering fix): "
            f"{card.get('recommended_action') or ''}\nSam replies to the "
            f"client from sprint item {adopted['item']}.")
        return {"ok": True, "did": f"sprint item {adopted['item']} (draft "
                                   f"{adopted['draft']}), {released}"}

    if action == "close":
        notes = []
        if (entry.get("zoho_ticket_id")
                and record.get("zoho_status") not in ZOHO_DONE):
            import zoho_actions

            zoho_actions.set_status(entry["zoho_ticket_id"], "Closed")
            notes.append("Zoho closed with no closure email")
        notes.append(release_task(
            entry, f"Nothing is owed here: "
            f"{card.get('situation') or card.get('recommended_action')}"))
        return {"ok": True, "did": ", ".join(notes)}

    if action == "fix":
        if not entry.get("zoho_ticket_id"):
            return {"ok": False, "why": "no client ticket: fix this one by "
                                        "hand in a session"}
        import ticket_fix

        fix = ticket_fix.propose_for_ticket(
            entry["zoho_ticket_id"], record.get("ticket_number")
            or entry["ticket_number"], record.get("account") or "", card,
            _zoho_url(entry["zoho_ticket_id"]), sam_approved=True)
        if not fix.get("pr_url"):
            if entry.get("task_id"):
                _comment(entry["task_id"],
                         f"The agent tried to fix this and did not open a "
                         f"PR ({fix.get('reason') or fix.get('outcome')}), "
                         "so it stays with you.\n\n"
                         + first_pass_text(record))
            why = fix.get("reason") or fix.get("outcome")
            return {"ok": False, "why": f"no PR: {why}"}
        parent = fix.get("parent")
        # The fix took the engineer task over as its child: closing it
        # now would close the task the release watcher tells the client
        # from (Sam's "moved, never copied" rule).
        if entry.get("task_id") and fix.get("child") == entry["task_id"]:
            return {"ok": True, "did": f"PR {fix['pr_url']}, "
                                       f"{entry['task_id']} moved in as "
                                       f"the child of {parent}"}
        released = release_task(
            entry, f"The agent opened a fix: {fix['pr_url']}"
            + (f"\nAgent Fixes: https://app.clickup.com/t/{parent}"
               if parent else "")
            + "\nSam merges it into the dev branch; the release watcher "
              "tells the client when it is live.")
        return {"ok": True, "did": f"PR {fix['pr_url']}, {released}"}

    return {"ok": False, "why": f"unknown action {action}"}


def run_apply(action: str, keys: list[str], confirm: bool,
              started: bool) -> int:
    if action not in ACTIONS:
        print(f"Action must be one of {', '.join(ACTIONS)}.")
        return 1
    if not os.environ.get("RAILWAY_ENVIRONMENT"):
        print("apply writes to ClickUp, Zoho and the sprint database, so it "
              "runs only with the worker's environment:\n  railway run "
              "--service vome-os-worker py backlog_sweep.py apply ...")
        return 1
    inv = _load(INVENTORY, {"entries": []})
    cards = _load(CARDS, {})
    applied = _load(APPLIED, {})
    by_key = {e["key"]: e for e in inv["entries"]}
    by_number = {e["ticket_number"]: e for e in inv["entries"]
                 if e["ticket_number"]}

    plan = []
    for key in keys:
        entry = by_key.get(key) or by_number.get(key.lstrip("#"))
        if not entry:
            print(f"  {key}: not in the inventory")
            continue
        record = cards.get(entry["key"])
        if not record or record.get("status") != "ok":
            print(f"  {key}: not triaged")
            continue
        if entry["key"] in applied:
            print(f"  {key}: already applied "
                  f"({applied[entry['key']].get('action')})")
            continue
        if entry["started"] and not started:
            print(f"  {key}: {', '.join(entry['engineers'])} has started it."
                  " Ask them first, then pass --started")
            continue
        plan.append((entry, record))

    for entry, record in plan:
        group, _ = bucket(entry, record)
        print(f"  {action} {entry['key']} #{entry['ticket_number']} "
              f"(sheet group: {group}) {entry['name'][:60]}")
    if not plan:
        return 1
    if not confirm:
        print("Dry run. Add --confirm to do it.")
        return 0

    failures = 0
    for entry, record in plan:
        try:
            result = apply_one(action, entry, record)
        except Exception as exc:
            result = {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
        if result.get("ok"):
            applied[entry["key"]] = {"action": action, "at": _now(),
                                     "did": result.get("did")}
            _save(APPLIED, applied)
            print(f"  done {entry['key']}: {result.get('did')}")
        else:
            failures += 1
            print(f"  NOT done {entry['key']}: {result.get('why')}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 5. Audit: is Zoho telling the same story as ClickUp?
# ---------------------------------------------------------------------------
#
# Sam, 2026-10-24: "Everything on Zoho and ClickUp in sync perfectly." The
# inventory above only sees `queued` and `in progress` engineer tasks; that
# day 38 of the 89 tasks on OnlyG's and Sanjay's plates sat at `awaiting
# client response` and no sweep had ever read them. The audit joins every
# open Zoho ticket, every open Priority Queue, Agent Fixes and Sam Accepted
# task and the sprint store, and gives each row one drift class. Reads only.

AUDIT = "audit.json"
AUDIT_SHEET = "audit.md"
AGENT_FIXES_LIST = "901115573468"
SAM_ACCEPTED_LIST = "901115573620"
ZOHO_OPEN = ("New", "Processing", "In Progress", "Awaiting Client Response",
             "On Hold")
ZOHO_OWED = ("New", "Processing")
ZOHO_AWAITING = "Awaiting Client Response"
ZOHO_GONE = "gone"
TASK_DONE = ("on prod", "closed", "done", "complete", "declined",
             "finished")
# Cards from before the deep pass shipped (2026-10-23 15:36 ET) are stale.
DEEP_CUTOFF = "2026-10-23T19:36:00+00:00"
STALL_DAYS = 14
# The `on prod` email goes out when the task reaches `on prod`, which is
# often hours before someone closes it: a reply that recent is the
# resolution, not an earlier answer (#7922 on 2026-10-24).
TOLD_SLACK = timedelta(days=1)
# Closed Priority Queue tasks are read back this far for drift class 1.
CLOSED_LOOKBACK_DAYS = 200

DRIFT = {
    1: ("untold", "Engineer task done, Zoho open, client never told"),
    2: ("no_task", "Zoho In Progress with no open task and no fix"),
    3: ("stalled", "Open engineer task untouched 14+ days, or unassigned"),
    4: ("gone", "Sprint item whose Zoho ticket no longer exists"),
    5: ("answered_owed", "We answered last, but Zoho says New or Processing"),
    6: ("client_wrote", "Awaiting Client Response, but the client wrote "
                        "since"),
    7: ("owner", "Zoho owner does not match the ClickUp work"),
    8: ("orphan", "Open ClickUp work whose Zoho ticket is Closed or gone"),
    9: ("duplicate", "More than one live task for one ticket"),
    10: ("uncarded", "Engineer item with no card from after the deep pass"),
    11: ("fix_waiting", "Agent fix open, waiting to go live"),
    12: ("healthy", "In sync"),
}
# The first class that applies is the row's class; the rest are kept as
# flags. A ticket that is gone or closed makes every other question moot,
# and duplicates have to be settled before anything is moved.
PRECEDENCE = (4, 8, 9, 1, 6, 5, 7, 2, 3, 10, 11)
# Classes whose rows may need a fresh card before anything is done.
TRIAGE_CLASSES = (1, 2, 3, 6, 10)


def _when(value) -> datetime | None:
    """An ISO string, a ClickUp millisecond stamp or a datetime, as UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _engineers_on(task: dict) -> list[str]:
    return [ENGINEERS[a] for a in task.get("assignees") or []
            if a in ENGINEERS]


def zoho_owner(zoho: dict) -> str:
    """onlyg / sanjay / agent_fixes / sam / other / '' for a Zoho row."""
    import zoho_actions

    if str(zoho.get("teamId") or "") == zoho_actions.AGENT_FIXES_TEAM:
        return "agent_fixes"
    assignee = str(zoho.get("assigneeId") or "")
    if assignee in ZOHO_TEAMS:
        return ZOHO_TEAMS[assignee]
    if assignee == zoho_actions.SAM_AGENT:
        return "sam"
    return "other" if assignee else ""


def drift(row: dict, now: datetime) -> list[tuple[int, str]]:
    """Every drift class that applies to one joined row, with the reason.

    `row` is what `join_audit` builds: the Zoho side (`zoho_status` is ''
    when the work names no ticket, `gone` when Zoho returns 404), the
    last inbound and outbound message times, and the ClickUp work as
    plain dicts (`id`, `status`, `assignees` as ids, `updated`, `closed`).
    """
    found: list[tuple[int, str]] = []
    status = row.get("zoho_status") or ""
    owner = row.get("zoho_owner") or ""
    pq_open = row.get("pq_open") or []
    pq_done = row.get("pq_done") or []
    fixes = row.get("fix_parents") or []
    children = row.get("fix_children") or []
    accepted = row.get("accepted") or []
    has_fix = bool(fixes or children)
    open_work = bool(pq_open or has_fix or accepted)
    last_in = _when(row.get("last_in"))
    last_out = _when(row.get("last_out"))
    engineers = sorted({e for t in pq_open for e in _engineers_on(t)})
    ticket_open = status in ZOHO_OPEN

    if status == ZOHO_GONE and row.get("sprint_open"):
        found.append((4, "the sprint still lists it, Zoho returns 404"))
    if status in ("Closed", ZOHO_GONE) and open_work:
        ids = [t["id"] for t in pq_open + fixes + children + accepted]
        found.append((8, f"Zoho {status}, still open in ClickUp: "
                         + ", ".join(ids)))

    if len(pq_open) > 1:
        found.append((9, f"{len(pq_open)} open Priority Queue tasks: "
                         + ", ".join(t["id"] for t in pq_open)
                         + " (a second issue may be legitimate)"))
    if len(children) > 1:
        found.append((9, f"{len(children)} live Agent Fixes children: "
                         + ", ".join(t["id"] for t in children)))
    if pq_open and children:
        found.append((9, "an open engineer task and an Agent Fixes child "
                         "both carry it (copied, not moved)"))

    if ticket_open and not open_work and pq_done:
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        done = max(pq_done, key=lambda t: _when(
            t.get("closed") or t.get("updated")) or oldest)
        done_at = _when(done.get("closed") or done.get("updated"))
        told = last_out and done_at and last_out > done_at - TOLD_SLACK
        if not told:
            found.append((1, f"task {done['id']} is {done['status']} since "
                             f"{done_at.date() if done_at else '?'}, last "
                             "reply to the client "
                             f"{last_out.date() if last_out else 'never'}"))
        elif status == "In Progress":
            found.append((2, f"task {done['id']} is {done['status']} and "
                             "the client was answered since, but Zoho is "
                             "still In Progress"))

    if status == ZOHO_AWAITING and last_in and (not last_out
                                                or last_in > last_out):
        found.append((6, f"client wrote {last_in.date()}, our last reply "
                         f"{last_out.date() if last_out else 'never'}"))
    if status in ZOHO_OWED and last_out and (not last_in
                                             or last_out > last_in):
        found.append((5, f"we replied {last_out.date()} after the client's "
                         f"{last_in.date() if last_in else 'first'} "
                         "message"))

    if status == "In Progress":
        if has_fix and owner != "agent_fixes":
            found.append((7, f"agent fix open, Zoho owner is "
                             f"{owner or 'nobody'}, not the Agent Fixes "
                             "team"))
        elif not has_fix and owner == "agent_fixes":
            found.append((7, "on the Agent Fixes team with no open fix"))
        elif not has_fix and engineers and owner not in engineers:
            found.append((7, f"ClickUp has {'/'.join(engineers)}, Zoho "
                             f"owner is {owner or 'nobody'}"))
        elif (not has_fix and not engineers and accepted
              and owner in ("onlyg", "sanjay")):
            found.append((7, f"Sam is building it, Zoho owner is {owner}"))

    if status == "In Progress" and not open_work and not pq_done:
        found.append((2, "no task and no fix"))

    for task in pq_open:
        updated = _when(task.get("updated"))
        if not task.get("assignees"):
            found.append((3, f"task {task['id']} has nobody on it"))
        elif (_engineers_on(task) and updated
              and (now - updated).days >= STALL_DAYS):
            found.append((3, f"task {task['id']} untouched since "
                             f"{updated.date()}"))

    engineer_item = bool(engineers) or (status == "In Progress"
                                        and owner in ("onlyg", "sanjay"))
    if engineer_item and not row.get("carded_after_cutoff"):
        found.append((10, "no card since the deep pass"
                          + ("" if status else " (no Zoho ticket)")))

    if fixes:
        checks = ", ".join(f"{t['id']} {t.get('check') or 'no check'}"
                           for t in fixes)
        found.append((11, f"fix {checks}"))
    return found


def classify_row(row: dict, now: datetime) -> dict:
    """The row with its class, reason and the other classes as flags."""
    found = drift(row, now)
    by_class: dict[int, list[str]] = {}
    for cls, why in found:
        by_class.setdefault(cls, []).append(why)
    primary = next((c for c in PRECEDENCE if c in by_class), 12)
    row = dict(row)
    row["drift"] = primary
    row["why"] = "; ".join(by_class.get(primary) or ["in sync"])
    row["flags"] = {c: by_class[c] for c in sorted(by_class) if c != primary}
    return row


def _slim(task: dict) -> dict:
    """The fields of a ClickUp task the audit reads."""
    from ticket_fix import _PR_LINE

    pr = _PR_LINE.search(task.get("description")
                         or task.get("text_content") or "")
    return {
        "id": str(task.get("id")),
        "name": (task.get("name") or "")[:140],
        "status": ((task.get("status") or {}).get("status") or "").lower(),
        "assignees": [a.get("id") for a in task.get("assignees") or []],
        "updated": task.get("date_updated") or "",
        "closed": task.get("date_closed") or task.get("date_done") or "",
        "parent": task.get("parent") or "",
        "pr": pr.group(1) if pr else "",
    }


def _is_open(task: dict) -> bool:
    return task["status"] not in TASK_DONE


def join_audit(zoho_rows: list[dict], exchanges: dict, lookups: dict,
               pq: list[dict], fixes: list[dict], accepted: list[dict],
               sprint_items: list[dict], carded: set[str]) -> list[dict]:
    """One row per Zoho ticket that is open or has open work, plus one per
    open engineer task that names no ticket.

    zoho_rows: open tickets from Zoho. exchanges: ticket id -> (last in,
    last out). lookups: ticket id -> status for tickets outside the open
    set ('Closed', 'gone', ...). Tasks are raw ClickUp tasks. carded:
    ticket ids and task ids with a card from after DEEP_CUTOFF.
    """
    from zoho_links import extract_zoho_ticket_id

    def index(tasks):
        by_id: dict[str, list] = {}
        by_number: dict[str, list] = {}
        for task in tasks:
            slim = _slim(task)
            tid = extract_zoho_ticket_id(task) or ""
            slim["ticket_id"] = tid
            if tid:
                by_id.setdefault(tid, []).append(slim)
            for number in _TICKET_IN_NAME.findall(task.get("name") or ""):
                by_number.setdefault(number, []).append(slim)
            if not tid and not _TICKET_IN_NAME.search(task.get("name") or ""):
                by_id.setdefault("", []).append(slim)
        return by_id, by_number

    pq_id, pq_num = index(pq)
    fx_id, fx_num = index(fixes)
    ac_id, ac_num = index(accepted)
    parents = {t["id"]: t for t in map(_slim, fixes) if not t["parent"]}
    sprint_open = {str(i.get("ticket_id")): i for i in sprint_items}

    tickets: dict[str, dict] = {}
    for zrow in zoho_rows:
        tickets[str(zrow.get("id"))] = {
            "ticket_id": str(zrow.get("id")),
            "ticket_number": str(zrow.get("ticketNumber") or ""),
            "subject": (zrow.get("subject") or "")[:140],
            "zoho_status": zrow.get("status") or "",
            "zoho_owner": zoho_owner(zrow),
        }
    # Tickets outside the open set that open work or the sprint still
    # points at: Closed, gone, or a status the audit did not list.
    for source in (pq_id, fx_id, ac_id):
        for tid in source:
            if tid and tid not in tickets and tid in lookups:
                tickets[tid] = {"ticket_id": tid, "ticket_number": "",
                                "subject": "", "zoho_owner": "",
                                "zoho_status": lookups[tid]}
    for tid in sprint_open:
        if tid not in tickets and tid in lookups:
            item = sprint_open[tid]
            tickets[tid] = {"ticket_id": tid,
                            "ticket_number": str(item.get("ticket_number")
                                                 or ""),
                            "subject": (item.get("subject") or "")[:140],
                            "zoho_owner": "", "zoho_status": lookups[tid]}

    def unique(tasks):
        return list({t["id"]: t for t in tasks}.values())

    rows = []
    for tid, row in tickets.items():
        number = row["ticket_number"]
        pq_tasks = unique(pq_id.get(tid, []) + (pq_num.get(number, [])
                                                if number else []))
        fx_tasks = unique(fx_id.get(tid, []) + (fx_num.get(number, [])
                                                if number else []))
        ac_tasks = unique(ac_id.get(tid, []) + (ac_num.get(number, [])
                                                if number else []))
        children = [t for t in fx_tasks if t["parent"] and _is_open(t)]
        fix_parents = {t["id"]: t for t in fx_tasks
                       if not t["parent"] and _is_open(t)}
        for child in children:
            parent = parents.get(child["parent"])
            if parent and _is_open(parent):
                fix_parents[parent["id"]] = parent
        last_in, last_out = exchanges.get(tid, ("", ""))
        row.update({
            "pq_open": [t for t in pq_tasks if _is_open(t)],
            "pq_done": [t for t in pq_tasks if not _is_open(t)
                        and _engineers_on(t)],
            "fix_parents": [dict(t, check=lookups.get(f"check:{t['id']}",
                                                      ""))
                            for t in fix_parents.values()],
            "fix_children": children,
            "accepted": [t for t in ac_tasks if _is_open(t)],
            "sprint_open": tid in sprint_open,
            "last_in": last_in,
            "last_out": last_out,
            "carded_after_cutoff": tid in carded,
        })
        if row["zoho_status"] not in ZOHO_OPEN and not (
                row["pq_open"] or row["fix_parents"] or row["fix_children"]
                or row["accepted"] or row["sprint_open"]):
            continue
        rows.append(row)

    # Engineer work that names no ticket at all (internal builds, Sentry).
    for task in pq_id.get("", []):
        if not _is_open(task) or not _engineers_on(task):
            continue
        rows.append({
            "ticket_id": "", "ticket_number": "", "zoho_status": "",
            "zoho_owner": "", "subject": task["name"], "pq_open": [task],
            "pq_done": [], "fix_parents": [], "fix_children": [],
            "accepted": [], "sprint_open": False, "last_in": "",
            "last_out": "", "carded_after_cutoff": task["id"] in carded,
        })
    return rows


def _list_open_and_recent(list_id: str, days: int) -> list[dict]:
    import ticket_reconciler

    since = int((time.time() - days * 86400) * 1000)
    return ticket_reconciler._list_tasks(list_id, since)


def _open_tasks(list_id: str) -> list[dict]:
    tasks: list[dict] = []
    for page in range(30):
        resp = httpx.get(f"{CLICKUP_BASE}/list/{list_id}/task",
                         headers=_clickup_headers(),
                         params={"page": page, "include_closed": "false",
                                 "subtasks": "true"}, timeout=30)
        resp.raise_for_status()
        batch = resp.json().get("tasks") or []
        tasks.extend(batch)
        if len(batch) < 100:
            break
    return tasks


def _exchange(ticket_id: str) -> tuple[str, str]:
    import sprint_batch
    import zoho_actions

    inbound, outbound = zoho_actions.last_exchange(
        ticket_id, skip=sprint_batch._out_of_office)
    return (str(inbound.get("createdTime") or ""),
            str(outbound.get("createdTime") or ""))


def _lookup_status(ticket_id: str) -> str:
    import zoho_actions

    try:
        return zoho_actions.ticket_status(ticket_id) or "unknown"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 422):
            return ZOHO_GONE
        return f"unreadable ({exc.response.status_code})"


def _sprint_open_items() -> list[dict]:
    import sprint_store
    from vomeos import store

    if not sprint_store.ready():
        raise RuntimeError("the sprint store is not reachable")
    return store.query(
        f"SELECT id, number, ticket_id, ticket_number, subject, status,"
        f" category, draft_status, updated_at FROM {sprint_store.ITEMS}"
        " WHERE status IN ('open', 'held', 'fixing')")


def _carded_since(cutoff: str) -> set[str]:
    """Ticket ids (and backlog task keys) carded after the deep pass."""
    import deep_triage
    from vomeos import store

    rows = store.query(
        f"SELECT DISTINCT ticket_id FROM {deep_triage.TABLE}"
        " WHERE created_at >= :c", {"c": _when(cutoff).replace(tzinfo=None)})
    carded = {str(r["ticket_id"]) for r in rows if r.get("ticket_id")}
    for key, record in (_load(CARDS, {}) or {}).items():
        if (record.get("triaged_at") or "") >= cutoff:
            carded.add(key.removeprefix("zoho:"))
    return carded


def triage_unit_cost(cutoff: str) -> dict:
    """What one triage cost since the deep pass shipped, from the runs."""
    import deep_triage
    from vomeos import store

    rows = store.query(
        "SELECT COUNT(*) AS n,"
        " AVG(COALESCE(first_usd, 0) + COALESCE(deep_usd, 0)) AS avg_all,"
        " AVG(CASE WHEN deep_usd > 0 THEN COALESCE(first_usd, 0) + deep_usd"
        " END) AS avg_deep,"
        " MAX(COALESCE(first_usd, 0) + COALESCE(deep_usd, 0)) AS max_one"
        f" FROM {deep_triage.TABLE} WHERE created_at >= :c",
        {"c": _when(cutoff).replace(tzinfo=None)})
    row = rows[0] if rows else {}
    return {k: float(row.get(k) or 0) for k in ("n", "avg_all", "avg_deep",
                                                "max_one")}


def build_audit(progress=print) -> dict:
    import release_watcher
    import ticket_reconciler

    progress("Zoho: open tickets")
    zoho_rows = []
    for status in ZOHO_OPEN:
        try:
            zoho_rows.extend(ticket_reconciler.zoho_tickets(status))
        except RuntimeError as exc:
            # An empty status comes back as no data at all.
            if "NoneType" not in str(exc):
                raise
    progress(f"  {len(zoho_rows)} open")

    progress("ClickUp: Priority Queue (open, and closed in the last "
             f"{CLOSED_LOOKBACK_DAYS} days), Agent Fixes, Sam Accepted")
    pq = _list_open_and_recent(PRIORITY_QUEUE, CLOSED_LOOKBACK_DAYS)
    fixes = _list_open_and_recent(AGENT_FIXES_LIST, CLOSED_LOOKBACK_DAYS)
    accepted = _open_tasks(SAM_ACCEPTED_LIST)
    progress(f"  {len(pq)} PQ, {len(fixes)} Agent Fixes, {len(accepted)} "
             "Sam Accepted")

    sprint_items = _sprint_open_items()
    carded = _carded_since(DEEP_CUTOFF)
    progress(f"Sprint: {len(sprint_items)} open items; {len(carded)} "
             "tickets carded since the deep pass")

    from zoho_links import extract_zoho_ticket_id

    open_ids = {str(r.get("id")) for r in zoho_rows}
    lookups: dict[str, str] = {}
    pointed = set()
    for task in pq + fixes + accepted:
        status = ((task.get("status") or {}).get("status") or "").lower()
        tid = extract_zoho_ticket_id(task) or ""
        if tid and status not in TASK_DONE:
            pointed.add(tid)
    pointed |= {str(i.get("ticket_id")) for i in sprint_items
                if i.get("ticket_id")}
    outside = sorted(pointed - open_ids)
    progress(f"Zoho: status of {len(outside)} ticket(s) open work points "
             "at outside the open set")
    for tid in outside:
        lookups[tid] = _lookup_status(tid)
    for task in fixes:
        if not task.get("parent"):
            lookups[f"check:{task.get('id')}"] = (
                release_watcher._release_check_value(task) or "")

    progress(f"Zoho: last message each way on {len(zoho_rows)} tickets")
    exchanges = {}
    for index, zrow in enumerate(zoho_rows, start=1):
        tid = str(zrow.get("id"))
        try:
            exchanges[tid] = _exchange(tid)
        except Exception as exc:
            exchanges[tid] = ("", "")
            progress(f"  {tid}: threads unreadable ({str(exc)[:80]})")
        if index % 25 == 0:
            progress(f"  {index}/{len(zoho_rows)}")

    now = datetime.now(timezone.utc)
    rows = [classify_row(r, now) for r in join_audit(
        zoho_rows, exchanges, lookups, pq, fixes, accepted, sprint_items,
        carded)]
    return {"generated_at": _now(), "cutoff": DEEP_CUTOFF,
            "unit_cost": triage_unit_cost(DEEP_CUTOFF), "rows": rows,
            "fix_parents_without_pr": [
                t["id"] for t in map(_slim, fixes)
                if not t["parent"] and not t["pr"] and _is_open(t)]}


def _row_label(row: dict) -> str:
    task = (row.get("pq_open") or row.get("fix_children")
            or row.get("accepted") or [{}])[0]
    number = f"#{row['ticket_number']}" if row.get("ticket_number") else ""
    owners = sorted({e for t in row.get("pq_open") or []
                     for e in _engineers_on(t)})
    bits = [number or f"task {task.get('id', '')}",
            row.get("zoho_status") or "no ticket"]
    if owners:
        bits.append("/".join(owners))
    if task.get("status"):
        bits.append(f"task {task['status']}")
    subject = row.get("subject") or task.get("name") or ""
    return f"{' | '.join(bits)} | {subject[:80]}"


def render_audit(audit: dict) -> str:
    """The review sheet: one section per drift class, counts first."""
    rows = audit["rows"]
    unit = audit.get("unit_cost") or {}
    by_class: dict[int, list] = {}
    for row in rows:
        by_class.setdefault(row["drift"], []).append(row)
    lines = [f"# Pipeline audit {audit['generated_at'][:16]} UTC", "",
             f"{len(rows)} rows. Cards older than {audit['cutoff'][:16]} "
             "UTC count as stale.", "", "| Class | Rows |", "|---|---|"]
    for cls, (_, title) in DRIFT.items():
        lines.append(f"| {cls}. {title} | {len(by_class.get(cls, []))} |")

    need = [r for r in rows if r["drift"] in TRIAGE_CLASSES
            and not r.get("carded_after_cutoff")]
    with_ticket = [r for r in need if r.get("ticket_id")]
    low = len(with_ticket) * (unit.get("avg_all") or 0.3)
    high = len(with_ticket) * (unit.get("avg_deep") or 1.5)
    lines += ["", f"**Triage estimate.** {len(with_ticket)} ticket rows in "
              f"classes {', '.join(map(str, TRIAGE_CLASSES))} have no fresh "
              f"card: about ${low:.0f} to ${high:.0f} (average "
              f"${unit.get('avg_all', 0):.2f} a run, "
              f"${unit.get('avg_deep', 0):.2f} with the deep pass, over "
              f"{int(unit.get('n', 0))} runs since the cutoff). "
              f"{len(need) - len(with_ticket)} more name no Zoho ticket."]
    if audit.get("fix_parents_without_pr"):
        lines += ["", "**Agent Fixes parents with no `Fix PR:` line:** "
                  + ", ".join(audit["fix_parents_without_pr"])]

    for cls, (_, title) in DRIFT.items():
        members = by_class.get(cls) or []
        if not members:
            continue
        lines += ["", f"## {cls}. {title} ({len(members)})", ""]
        for row in sorted(members, key=lambda r: r.get("ticket_number")
                          or "~"):
            flags = " ".join(f"[{c}]" for c in row.get("flags") or {})
            lines.append(f"- {_row_label(row)} -- {row['why']}"
                         + (f" {flags}" if flags else ""))
    return "\n".join(lines) + "\n"


def run_audit(reuse: bool = False) -> int:
    if reuse:
        # Classify the rows of the last run again, without reading Zoho or
        # ClickUp: for a change to the rules, not for a fresh picture.
        audit = _load(AUDIT, None)
        if not audit:
            print("No audit yet. Run `backlog_sweep.py audit` first.")
            return 1
        now = _when(audit["generated_at"])
        audit["rows"] = [classify_row(r, now) for r in audit["rows"]]
    else:
        audit = build_audit()
    _save(AUDIT, audit)
    sheet = render_audit(audit)
    _path(AUDIT_SHEET).write_text(sheet, encoding="utf-8", newline="\n")
    print(sheet.split("\n## ")[0])
    print(f"Wrote {_path(AUDIT)} and {_path(AUDIT_SHEET)}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory")
    p_inv.add_argument("--zoho-pages", type=int, default=ZOHO_PAGES)
    p_inv.add_argument("--include-triaged", action="store_true")

    p_tri = sub.add_parser("triage")
    p_tri.add_argument("--limit", type=int, default=0)
    p_tri.add_argument("--only", nargs="*", default=[])
    p_tri.add_argument("--refresh", action="store_true")

    sub.add_parser("sheet")
    p_aud = sub.add_parser("audit")
    p_aud.add_argument("--reuse", action="store_true")

    p_app = sub.add_parser("apply")
    p_app.add_argument("action", choices=ACTIONS)
    p_app.add_argument("keys", nargs="+")
    p_app.add_argument("--confirm", action="store_true")
    p_app.add_argument("--started", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "inventory":
        inv = build_inventory(args.zoho_pages, args.include_triaged)
        _save(INVENTORY, inv)
        print(summarise_inventory(inv))
        print(f"Wrote {_path(INVENTORY)}")
        return 0
    if args.cmd == "triage":
        return run_triage(args.limit, args.only, args.refresh)
    if args.cmd == "sheet":
        return run_sheet()
    if args.cmd == "audit":
        return run_audit(args.reuse)
    return run_apply(args.action, args.keys, args.confirm, args.started)


if __name__ == "__main__":
    sys.exit(main())
