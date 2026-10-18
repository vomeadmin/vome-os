"""
ticket_fix.py

When triage reads the code and finds a small, certain bug behind a customer
ticket, propose the fix as a pull request into the dev branch, and file it
in Agent Fixes so the release watcher can carry it to the customer.

WHY THIS EXISTS
---------------
Sam's rule (2026-10-16): if there is a fix, make it. Until now that happened
only when he and an agent sat down in a coding session. The Sentry pipeline
already turns a diagnosis into a checked diff and a pull request; this points
the same machinery at the bugs customers report.

THE LADDER (each rung can stop it, and usually does)
----------------------------------------------------
1. The triage card's code_check says `bug`, confidence high, fix_risk low,
   with a file and a described fix. Anything else stops here.
2. The code around that line is read on the live branch.
3. `engineering.fix_author` writes a diff, or declines.
4. `patch_safety` refuses migrations, settings, auth, payments,
   dependencies, CI and tests. The diff must apply to the dev branch as it
   is now.
5. A pull request opens into `development` (backend) or `develop` (web).
   It is never merged by an agent: Sam merges agent PRs into the dev
   branch (they carry no migrations), and the Merge Owner field says so.
   Production: Sam deploys web, OnlyG merges core to master.

Then, in Agent Fixes: a parent task (the fix, no Zoho link, "Fix PR:" in its
description) and a child per ticket (with the Zoho link). Once the pull
request merges, `promote_merged` writes the merge commit into Fix Commit and
moves the parent and its children to `on dev`, which is where the release
watcher picks them up and proves them live. Nothing here ever sets `on prod`.

SWITCHES
--------
    TICKET_FIX_ENABLED     default on. "false" proposes nothing.
    TICKET_FIX_DAILY_CAP   pull requests per day, default 3.
    SENTRY_AUTO_PR_ENABLED the shared kill switch for agent-opened PRs.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx

from vomeos import store

AGENT_FIXES_LIST = "901115573468"
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"
FIELD_FIX_COMMIT = "ee6d6f9b-2093-40df-846c-4c5e7003f2c5"
FIELD_MERGE_OWNER = "12682083-5a03-4f27-a6b6-5c716aa5dd73"
FIELD_RELEASE_CHECK = "6d2d0de6-298f-40b3-b628-ab2e9633f6e7"
RELEASE_WAITING = "003c8ff6-370b-4f8f-8493-db7da609a033"
FIELD_TAGS = "291fd0e7-42e3-4376-bacd-2b54a6c1d48c"
TAG_AGENT = "d4cf0a1c-7f21-41c5-8b8b-f00ebb459552"
FIELD_TYPE = "e0e439f5-397d-432d-addd-e90fbf50cd30"
TYPE_BUG = "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df"

# Sam merges every agent PR into the dev branch (2026-10-18): they never
# carry a migration (patch safety refuses one), so no backend merge needs
# OnlyG. Production stays split: Sam deploys web, OnlyG merges core.
MERGE_OWNER = 3691763  # Sam

_PR_LINE = re.compile(r"Fix PR:\s*(\S+)")

LEDGER = "vomeos_ticket_fixes"
store.register_table(
    LEDGER,
    f"""
    CREATE TABLE IF NOT EXISTS {LEDGER} (
        ticket_id     VARCHAR PRIMARY KEY,
        ticket_number VARCHAR,
        attempted_at  TIMESTAMP NOT NULL DEFAULT NOW(),
        outcome       VARCHAR,
        reason        TEXT,
        pr_url        TEXT,
        parent_task   VARCHAR,
        child_task    VARCHAR
    )
    """,
)


def enabled() -> bool:
    value = os.environ.get("TICKET_FIX_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


def daily_cap() -> int:
    return int(os.environ.get("TICKET_FIX_DAILY_CAP", "3"))


# "core:form_app/s.py:120" or "web:src/x.js". A location with commentary
# after it ("web:src/views/org/orgSequences (exact render path not
# located...)", #9415) is not one a fix can be written against.
_EXACT_WHERE = re.compile(r"^[\w-]+:[^\s:()]+(:\d+(-\d+)?)?$")


def should_attempt(card: dict, sam_approved: bool = False
                   ) -> tuple[bool, str]:
    """Whether to write a fix and open a PR for this card.

    Sam, 2026-10-18: medium confidence is enough. The first 16 production
    cards had no high-confidence bug at all, while #9500 and #9415 (both
    medium) were real bugs that were then fixed by hand. A PR is a proposal
    OnlyG or Sanjay reviews, so a medium call costs a review, not a release.

    What is never fixed on the agent's own call: works_as_designed (the
    customer misread the feature: a how-to) and design_question (the code
    does what it was built to do, but that contradicts how the feature
    should work: Sam decides whether to change it, log it, or answer it).
    `sam_approved` is Sam saying `fix N`: it lifts the verdict and
    confidence bar, never the risk bar or the patch safety guard.
    """
    code = card.get("code_check") if isinstance(card, dict) else None
    if not isinstance(code, dict):
        return False, "no code check on the card"
    verdict = code.get("verdict")
    if not sam_approved:
        if verdict == "design_question":
            return False, "design question: needs Sam's decision"
        if verdict != "bug":
            return False, f"code check verdict is {verdict!r}"
        if code.get("confidence") not in ("high", "medium"):
            return False, "code check confidence is low"
    if code.get("fix_risk") != "low":
        return False, f"fix risk is {code.get('fix_risk')!r}, not low"
    if not code.get("fix") or not code.get("where"):
        return False, "no described fix or location"
    if not _EXACT_WHERE.match(str(code.get("where")).strip()):
        return False, f"location is not exact: {code.get('where')!r}"
    if sam_approved:
        return True, "Sam asked for the fix"
    return True, "small bug the code confirms"


def parse_where(where: str) -> tuple[str, str, int]:
    """"core:app/x.py:1071-1134" -> ("core", "app/x.py", 1071)."""
    parts = (where or "").strip().split(":")
    if len(parts) < 2:
        return "", "", 0
    alias, path = parts[0], parts[1]
    line = 0
    if len(parts) > 2:
        match = re.match(r"(\d+)", parts[2])
        line = int(match.group(1)) if match else 0
    return alias.strip(), path.strip(), line


def _attempted(ticket_id: str) -> bool:
    if not store.ensure_table(LEDGER):
        return False
    rows = store.query(f"SELECT 1 AS x FROM {LEDGER} WHERE ticket_id = :t",
                       {"t": ticket_id})
    return bool(rows)


def _opened_today() -> int:
    rows = store.query(
        f"SELECT COUNT(*) AS n FROM {LEDGER} WHERE outcome = 'pr_opened'"
        " AND attempted_at >= date_trunc('day', NOW())"
    )
    return int(rows[0]["n"]) if rows else 0


def _record(ticket_id: str, number: str, outcome: str, reason: str,
            pr_url: str = "", parent: str = "", child: str = "") -> None:
    if not store.ensure_table(LEDGER):
        return
    store.execute(
        f"INSERT INTO {LEDGER} (ticket_id, ticket_number, outcome, reason,"
        " pr_url, parent_task, child_task) VALUES (:t, :n, :o, :r, :p, :pa,"
        " :c) ON CONFLICT (ticket_id) DO UPDATE SET outcome = EXCLUDED."
        "outcome, reason = EXCLUDED.reason, pr_url = EXCLUDED.pr_url,"
        " parent_task = EXCLUDED.parent_task, child_task = EXCLUDED."
        "child_task",
        {"t": ticket_id, "n": number, "o": outcome, "r": reason[:1000],
         "p": pr_url, "pa": parent, "c": child},
    )


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------

def propose_for_ticket(ticket_id: str, ticket_number: str, account: str,
                       card: dict, zoho_url: str,
                       sam_approved: bool = False) -> dict:
    """Returns {"outcome", "reason", "pr_url"}. Never raises.

    One attempt per ticket on the agent's own call: a ticket that comes back
    with a new reply is not a reason to open a second pull request for the
    same bug. Sam's `fix N` may retry, and is not held to the daily cap.
    """
    try:
        return _propose(ticket_id, ticket_number, account, card, zoho_url,
                        sam_approved)
    except Exception as exc:
        print(f"[TICKET FIX] #{ticket_number} failed: {exc}")
        return {"outcome": "error", "reason": str(exc), "pr_url": ""}


def _propose(ticket_id, ticket_number, account, card, zoho_url,
             sam_approved=False) -> dict:
    if not enabled():
        return {"outcome": "skipped", "reason": "disabled", "pr_url": ""}
    ok, reason = should_attempt(card, sam_approved=sam_approved)
    if not ok:
        return {"outcome": "skipped", "reason": reason, "pr_url": ""}
    if not sam_approved and _attempted(ticket_id):
        return {"outcome": "skipped", "reason": "already attempted",
                "pr_url": ""}
    if not sam_approved and _opened_today() >= daily_cap():
        return {"outcome": "skipped", "reason": "daily cap reached",
                "pr_url": ""}

    import sentry_code
    import sentry_fix
    from vomeos.tools import repo_map

    code = card["code_check"]
    alias, path, line = parse_where(code.get("where"))
    repo = repo_map().get(alias)
    if not repo or not path:
        _record(ticket_id, ticket_number, "skipped",
                f"unknown location {code.get('where')!r}")
        return {"outcome": "skipped", "reason": "unknown location",
                "pr_url": ""}

    # The fix author reads the same windowed source the Sentry analyst does,
    # built from a one-frame trace at the line the code check named.
    trace = f'File "{path}", line {line or 1}, in ticket_{ticket_number}'
    gathered = sentry_code.gather(trace, repo.slug, repo.branch,
                                  host=repo.host, owner=repo.owner)
    analysis = {
        "root_cause": code.get("finding") or "",
        "proposed_fix": code.get("fix") or "",
        "risk": "low",
        # The real call, so the fix author weighs a medium one accordingly.
        "confidence": code.get("confidence") or "medium",
        "needs_migration": False,
        "files": [path],
    }
    issue_key = f"zoho-{ticket_number}"
    proposal = sentry_fix.propose(analysis, {
        "source": gathered.get("files", ""), "repo": repo.slug,
        "branch": repo.branch, "issue_id": issue_key,
    })
    if not proposal.get("diff"):
        _record(ticket_id, ticket_number, "declined",
                proposal.get("reason") or "no diff")
        return {"outcome": "declined", "reason": proposal.get("reason"),
                "pr_url": ""}

    summary = (card.get("live_question") or "")[:60]
    pr = sentry_fix.open_pull_request(
        {"issue_id": issue_key, "triage_summary": summary,
         "source_label": f"Zoho ticket #{ticket_number} ({account})",
         "severity": "customer reported"},
        proposal,
        {"repo": repo.slug, "host": repo.host, "owner": repo.owner,
         "root_cause": analysis["root_cause"]},
    )
    if not pr.get("opened"):
        _record(ticket_id, ticket_number, "not_opened",
                pr.get("reason") or "")
        return {"outcome": "not_opened", "reason": pr.get("reason"),
                "pr_url": ""}

    parent, child = _file_tasks(ticket_number, account, card, repo, pr["url"],
                                zoho_url)
    _record(ticket_id, ticket_number, "pr_opened", "", pr["url"], parent,
            child)
    return {"outcome": "pr_opened", "reason": "", "pr_url": pr["url"],
            "parent": parent, "child": child}


# ---------------------------------------------------------------------------
# ClickUp
# ---------------------------------------------------------------------------

def _clickup(method: str, path: str, body: dict | None = None) -> dict:
    resp = httpx.request(
        method, f"https://api.clickup.com/api/v2{path}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _file_tasks(ticket_number, account, card, repo, pr_url, zoho_url):
    code = card.get("code_check") or {}
    summary = (card.get("live_question") or "").strip()[:90]
    owner = MERGE_OWNER
    description = "\n".join([
        "**Agent fix proposed from a customer ticket.** Not reviewed by a "
        "person. Merge into the dev branch only after review.",
        "",
        f"Fix PR: {pr_url}",
        f"**Repo:** {repo.alias} ({repo.slug}), into the dev branch",
        "",
        "## Engineer summary",
        f"**Likely cause:** {code.get('finding') or ''}",
        f"**Where:** `{code.get('where') or ''}`",
        f"**Potential fix:** {code.get('fix') or ''}",
        f"**Confidence:** {code.get('confidence')} | "
        f"**Risk:** {code.get('fix_risk')}",
        "",
        "## Tickets",
        f"* #{ticket_number} {account}",
        "",
        "_When the PR merges, Fix Commit is filled in automatically and the "
        "release watcher takes it from there._",
    ])
    parent = _clickup("POST", f"/list/{AGENT_FIXES_LIST}/task", {
        "name": f"[#{ticket_number}] {summary} -- P3",
        "markdown_description": description,
        "status": "in progress",
        "custom_fields": [
            {"id": FIELD_TAGS, "value": [TAG_AGENT]},
            {"id": FIELD_TYPE, "value": TYPE_BUG},
            {"id": FIELD_MERGE_OWNER, "value": {"add": [owner]}},
            {"id": FIELD_RELEASE_CHECK, "value": RELEASE_WAITING},
        ],
    })
    child = _clickup("POST", f"/list/{AGENT_FIXES_LIST}/task", {
        "name": f"[#{ticket_number}] {account.split('(')[0].strip()} -- "
                f"{summary} (linked to fix)",
        "parent": parent.get("id"),
        "status": "in progress",
        "custom_fields": [{"id": FIELD_ZOHO_TICKET_LINK,
                           "value": zoho_url}],
    })
    return str(parent.get("id") or ""), str(child.get("id") or "")


# ---------------------------------------------------------------------------
# PR merged -> on dev
# ---------------------------------------------------------------------------

def _pr_merge_commit(pr_url: str) -> tuple[str, str] | None:
    """(alias, merge sha) once the PR has merged, else None."""
    from vomeos.integrations.code_search import Bitbucket, GitHub
    from vomeos.tools import repo_map

    by_slug = {r.slug: r for r in repo_map().values()}
    gh = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    bb = re.search(r"bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)",
                   pr_url)
    if gh:
        repo = by_slug.get(gh.group(2))
        result = GitHub().pull_request(gh.group(2), gh.group(3),
                                       owner=gh.group(1))
        if repo and result.ok and result.get("merged"):
            return repo.alias, str(result.get("merge_commit_sha") or "")[:9]
    elif bb:
        repo = by_slug.get(bb.group(2))
        result = Bitbucket().pull_request(bb.group(2), bb.group(3))
        if repo and result.ok and result.get("state") == "MERGED":
            commit = result.get("merge_commit") or {}
            return repo.alias, str(commit.get("hash") or "")[:8]
    return None


def _status(task: dict) -> str:
    """ClickUp gives status as an object on reads, a string on some paths."""
    status = task.get("status")
    if isinstance(status, dict):
        status = status.get("status")
    return str(status or "").strip().lower()


def promote_merged(tasks: list[dict]) -> list[str]:
    """Parents whose fix PR has merged get Fix Commit and move to on dev.

    `tasks` is the Agent Fixes list (any status). Returns what changed, for
    the log. Moves to `on dev` only, which fires nothing; the release watcher
    then proves the commit live before anyone tells the customer.
    """
    changed = []
    for task in tasks:
        if task.get("parent") or _status(task) != "in progress":
            continue
        fix_commit = next((f.get("value") for f in task.get("custom_fields")
                           or [] if f.get("id") == FIELD_FIX_COMMIT), "")
        if fix_commit:
            continue
        match = _PR_LINE.search(task.get("description")
                                or task.get("text_content") or "")
        if not match:
            continue
        merged = _pr_merge_commit(match.group(1))
        if not merged or not merged[1]:
            continue
        value = f"{merged[0]}@{merged[1]}"
        try:
            _clickup("POST", f"/task/{task['id']}/field/{FIELD_FIX_COMMIT}",
                     {"value": value})
            _clickup("PUT", f"/task/{task['id']}", {"status": "on dev"})
            for child in tasks:
                if child.get("parent") == task["id"]:
                    _clickup("PUT", f"/task/{child['id']}",
                             {"status": "on dev"})
            changed.append(f"{task['id']} merged as {value}")
        except Exception as exc:
            changed.append(f"{task['id']} promote failed: {exc}")
    return changed


def list_in_progress() -> list[dict]:
    """Agent Fixes tasks at `in progress`, parents and children."""
    data = _clickup(
        "GET",
        f"/list/{AGENT_FIXES_LIST}/task?statuses[]=in%20progress"
        "&subtasks=true&include_closed=false",
    )
    return data.get("tasks") or []


def run_promotion() -> dict:
    """Called by the release watcher each pass. Never raises."""
    try:
        changed = promote_merged(list_in_progress())
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if changed:
        print("[TICKET FIX] " + "; ".join(changed))
    return {"status": "ok", "promoted": changed,
            "at": datetime.now(timezone.utc).isoformat()}
