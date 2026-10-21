"""
backlog_sweep.py

Triage the work that reached the engineers before triage existed, and take
off their plates what an answer or an agent fix can settle.

    py backlog_sweep.py inventory              list the backlog, read only
    railway run --service vome-os-worker py backlog_sweep.py triage --limit 10
    py backlog_sweep.py sheet                  the review sheet, by group
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
from datetime import datetime, timezone
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
    record = {"key": entry["key"], "triaged_at": _now(), **facts,
              "status": result.status, "card": result.data or {}}
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
    return run_apply(args.action, args.keys, args.confirm, args.started)


if __name__ == "__main__":
    sys.exit(main())
