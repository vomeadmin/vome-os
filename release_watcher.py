"""
release_watcher.py

Notice when an agent fix reaches production, so the client can be told.

WHY THIS EXISTS
---------------
An agent fix is merged into a dev branch (`development` on Bitbucket,
`develop` on GitHub) and a person promotes it to `master` later, in a batch.
Until 2026-10-17 nothing watched `master`: the only automatic email was the
on-prod handler, which fires when someone moves a task to `on prod` by hand.
So a fix could sit live for days with its client still waiting, or, worse,
someone could move a task early and the handler would email a client about a
fix that had not shipped.

On 2026-10-16 that gap was visible on real tickets. Three fixes (#9415, #9436,
#9500) missed the evening promotion by 30 to 90 minutes, and the only way to
find out was a person running `git merge-base` by hand.

WHAT "LIVE" MEANS HERE
----------------------
A fix is live only when EVERY commit in its Fix Commit field is:

  1. merged: an ancestor of the repo's production branch, and
  2. deployed: inside the newest successful production build on that branch.
     For web, the build stamp the live site serves (`window.__VOME_BUILD__`
     in index.html) must also contain it, because a green CI run is not a
     CloudFront invalidation.

Merged is never inferred from "someone said they merged it". It is the
commit graph, read through the read-only connectors in
`vomeos/integrations/code_search.py`.

WHAT IT DOES AND DOES NOT DO
----------------------------
Every 15 minutes, for each Agent Fixes parent at `on dev` with Fix Commit set:

  * all commits deployed      -> Release Check = Live, one post in
                                 #support-sprints naming the clients to notify
  * a build carrying a commit -> Release Check = Failed, one alert with the
    failed, or merged 2h+ and    run link
    still not deployed
  * otherwise                 -> nothing (Waiting)

It NEVER moves a task's status. `on prod` fires the on-prod handler, which
writes and sends a client email with no person in the loop, and these fixes
were written by an agent. Resolution replies go through Sam in the sprint.
It never touches Zoho and never emails anyone.

A failed API call is not "not merged". When GitHub, Bitbucket or ClickUp does
not answer, the fix is reported as "could not check" and left as it is. The
measured worst failure of this operation is asserting a state no record backs.

STATE
-----
`vomeos_release_checks` keeps one row per (task, repo, sha) with the time it
was first seen merged and first seen deployed, plus the run that proved it.
`vomeos_release_fixes` keeps one row per task with the last state Slack was
told about. A post happens only when that state changes, so a restart or a
redeploy never re-alerts.

SWITCHES
--------
    RELEASE_WATCHER_ENABLED   default on. "false" stops the job entirely.
    RELEASE_WATCHER_TIMEOUT_MINUTES  merged-but-not-deployed alarm, default 120
    SLACK_CHANNEL_SUPPORT_SPRINTS    default C0CA1QB2XSQ

On demand:

    py release_watcher.py            print only: no Slack, no ClickUp writes
    py release_watcher.py --task ID  one parent
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from vomeos import store
from vomeos.integrations.code_search import Bitbucket, GitHub

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

AGENT_FIXES_LIST = "901115573468"
STATUS_ON_DEV = "on dev"

FIELD_FIX_COMMIT = "ee6d6f9b-2093-40df-846c-4c5e7003f2c5"
FIELD_RELEASE_CHECK = "6d2d0de6-298f-40b3-b628-ab2e9633f6e7"
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

RELEASE_CHECK_OPTIONS = {
    "waiting": "003c8ff6-370b-4f8f-8493-db7da609a033",
    "failed": "cbfc850e-c699-47a5-97a8-c1522ed5dede",
    "live": "92aa982c-d016-486f-a2c8-41f175a83459",
}

SPRINTS_CHANNEL = os.environ.get(
    "SLACK_CHANNEL_SUPPORT_SPRINTS", "C0CA1QB2XSQ"
)

# Pair states, per (repo, sha).
PENDING_MERGE = "pending_merge"   # not on the production branch yet
BUILDING = "building"             # merged, no successful build carries it yet
DEPLOYED = "deployed"             # in a successful build (and the live stamp)
BUILD_FAILED = "failed"           # merged, and the build carrying it failed
UNKNOWN = "unknown"               # a call failed; we do not know

# Fix states, per parent task.
FIX_LIVE = "live"
FIX_FAILED = "failed"
FIX_WAITING = "waiting"
FIX_UNKNOWN = "unknown"

_GH_FAILED = {"failure", "cancelled", "timed_out", "startup_failure"}
_BB_FAILED = {"FAILED", "ERROR", "STOPPED"}


@dataclass(frozen=True)
class Target:
    """Where one repo alias lives and how its production deploy is proven."""

    alias: str
    host: str          # "github" or "bitbucket"
    repo: str
    branch: str
    owner: str = ""    # GitHub only
    workflow: str = ""  # GitHub only: the prod workflow file
    stamp_url: str = ""  # web only: page carrying window.__VOME_BUILD__


TARGETS = {
    "core": Target(
        alias="core",
        host="bitbucket",
        repo="vomedjango-restored-core-app",
        branch="master",
    ),
    "web": Target(
        alias="web",
        host="github",
        owner="vomeadmin",
        repo="vome-react",
        branch="master",
        workflow="cicd-prod.yml",
        stamp_url="https://www.vomevolunteer.com/index.html",
    ),
}
# Other spellings people write in Fix Commit. Same names as the VomeOS repo
# map, so a task written from that page parses here.
ALIASES = {
    "backend": "core",
    "django-core": "core",
    "frontend": "web",
    "react": "web",
    "vome-react": "web",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flag(name: str) -> bool:
    """On unless explicitly switched off."""
    value = os.environ.get(name, "").strip().lower()
    return value not in ("false", "0", "no", "off")


def _enabled() -> bool:
    return _flag("RELEASE_WATCHER_ENABLED")


def _timeout() -> timedelta:
    return timedelta(
        minutes=int(os.environ.get("RELEASE_WATCHER_TIMEOUT_MINUTES", "120"))
    )


# ---------------------------------------------------------------------------
# Fix Commit parsing
# ---------------------------------------------------------------------------

_PAIR = re.compile(r"^([A-Za-z][\w-]*)@([0-9a-fA-F]{7,40})$")


def parse_fix_commit(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """`core@068fed0d; web@311a422d5` -> ([("core", "068fed0d"), ...], errors).

    Separators are `;`, `,` or whitespace. An unrecognised token is an error
    rather than silently dropped: a fix whose web commit was mistyped must not
    go Live on the strength of its core commit alone.
    """
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    for token in re.split(r"[;,\s]+", (text or "").strip()):
        if not token:
            continue
        match = _PAIR.match(token)
        if not match:
            errors.append(f"cannot read {token!r} (expected repo@sha)")
            continue
        alias = match.group(1).lower()
        alias = ALIASES.get(alias, alias)
        if alias not in TARGETS:
            errors.append(f"unknown repo {match.group(1)!r} in {token!r}")
            continue
        pair = (alias, match.group(2).lower())
        if pair not in pairs:
            pairs.append(pair)
    return pairs, errors


# ---------------------------------------------------------------------------
# Checking one commit
# ---------------------------------------------------------------------------

@dataclass
class PairCheck:
    alias: str
    sha: str
    state: str
    detail: str = ""
    run_id: str = ""
    run_url: str = ""
    live_sha: str = ""


class _Graph:
    """Ancestry questions against one repo, cached for the length of a pass."""

    def __init__(self, target: Target, github: GitHub, bitbucket: Bitbucket):
        self.target = target
        self.github = github
        self.bitbucket = bitbucket
        self.error = ""
        self._cache: dict[tuple[str, str], bool | None] = {}

    def contains(self, sha: str, head: str) -> bool | None:
        """True when `sha` is an ancestor of (or equal to) `head`.

        None when the answer could not be had, which callers must not read as
        False.
        """
        key = (sha, head)
        if key in self._cache:
            return self._cache[key]
        t = self.target
        if t.host == "github":
            result = self.github.compare(t.repo, sha, head, owner=t.owner)
            answer = (
                result.get("status") in ("ahead", "identical")
                if result.ok else None
            )
        else:
            result = self.bitbucket.merge_base(t.repo, sha, head)
            base = (result.get("hash") or "") if result.ok else ""
            answer = (
                base.lower().startswith(sha.lower()) if result.ok else None
            )
        if answer is None:
            self.error = result.error
        self._cache[key] = answer
        return answer


def _runs(target: Target, github: GitHub, bitbucket: Bitbucket):
    """Recent production builds on the branch, newest first, normalised.

    Each run: {"id", "head", "done", "ok", "failed", "url"}. Returns
    (runs, error); error set means the list could not be read.
    """
    if target.host == "github":
        result = github.workflow_runs(
            target.repo, target.workflow, branch=target.branch,
            owner=target.owner,
        )
        if not result.ok:
            return [], result.error
        runs = []
        for run in result.get("workflow_runs") or []:
            done = run.get("status") == "completed"
            conclusion = run.get("conclusion") or ""
            runs.append({
                "id": str(run.get("id") or ""),
                "head": run.get("head_sha") or "",
                "done": done,
                "ok": done and conclusion == "success",
                "failed": done and conclusion in _GH_FAILED,
                "url": run.get("html_url") or "",
            })
        return runs, ""

    result = bitbucket.pipelines(target.repo, target.branch)
    if not result.ok:
        return [], result.error
    runs = []
    for run in result.get("values") or []:
        state = run.get("state") or {}
        done = state.get("name") == "COMPLETED"
        outcome = (state.get("result") or {}).get("name") or ""
        number = run.get("build_number")
        runs.append({
            "id": str(number or run.get("uuid") or ""),
            "head": ((run.get("target") or {}).get("commit") or {}).get("hash")
            or "",
            "done": done,
            "ok": done and outcome == "SUCCESSFUL",
            "failed": done and outcome in _BB_FAILED,
            "url": (
                f"https://bitbucket.org/{bitbucket.workspace}/{target.repo}"
                f"/pipelines/results/{number}" if number else ""
            ),
        })
    return runs, ""


def _live_stamp(url: str) -> str:
    """The short sha the live web app was built from, or "" if unreadable."""
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[RELEASE] could not read live build stamp: {exc}")
        return ""
    match = re.search(r"__VOME_BUILD__\s*=\s*(\{[^<;]*\})", resp.text)
    if not match:
        return ""
    try:
        return str(json.loads(match.group(1)).get("sha") or "")
    except ValueError:
        return ""


def check_pair(
    alias: str,
    sha: str,
    *,
    github: GitHub,
    bitbucket: Bitbucket,
    graphs: dict[str, _Graph],
    runs_cache: dict[str, tuple[list, str]],
    stamp_cache: dict[str, str],
) -> PairCheck:
    """Where one commit is: pending merge, building, deployed, failed, unknown.

    Builds are walked newest first. The first run that does not contain the
    commit ends the walk, because every older run is older still. The first
    COMPLETED run that does contain it decides: success is deployed, failure
    is failed, unless a newer run carrying it is still going, which is a
    retry and therefore building.
    """
    target = TARGETS[alias]
    graph = graphs.setdefault(alias, _Graph(target, github, bitbucket))

    merged = graph.contains(sha, target.branch)
    if merged is None:
        return PairCheck(
            alias, sha, UNKNOWN, f"merge check failed: {graph.error}"
        )
    if not merged:
        return PairCheck(alias, sha, PENDING_MERGE, f"not on {target.branch}")

    if alias not in runs_cache:
        runs_cache[alias] = _runs(target, github, bitbucket)
    runs, error = runs_cache[alias]
    if error:
        return PairCheck(
            alias, sha, UNKNOWN, f"build list unreadable: {error}"
        )

    in_progress = None
    for run in runs:
        if not run["head"]:
            continue
        carried = graph.contains(sha, run["head"])
        if carried is None:
            return PairCheck(
                alias, sha, UNKNOWN, f"build check failed: {graph.error}"
            )
        if not carried:
            break
        if not run["done"]:
            in_progress = in_progress or run
            continue
        if run["ok"]:
            check = PairCheck(
                alias, sha, DEPLOYED, "build green",
                run_id=run["id"], run_url=run["url"],
            )
            return _confirm_live(check, target, graph, stamp_cache)
        if run["failed"]:
            if in_progress:
                return PairCheck(
                    alias, sha, BUILDING, "retry running after a failed build",
                    run_id=in_progress["id"], run_url=in_progress["url"],
                )
            return PairCheck(
                alias, sha, BUILD_FAILED, "production build failed",
                run_id=run["id"], run_url=run["url"],
            )

    if in_progress:
        return PairCheck(
            alias, sha, BUILDING, "production build running",
            run_id=in_progress["id"], run_url=in_progress["url"],
        )
    return PairCheck(alias, sha, BUILDING, "merged, no production build yet")


def _confirm_live(
    check: PairCheck,
    target: Target,
    graph: _Graph,
    stamp_cache: dict[str, str],
) -> PairCheck:
    """For web, the live site must be serving a build that contains the sha.

    An unreadable stamp does not block: the green build is still evidence,
    and the page may simply have changed shape. A readable stamp that does
    NOT contain the commit does block, because that is a positive sign the
    deploy has not reached users (CloudFront, a half-finished sync).
    """
    if not target.stamp_url:
        return check
    if target.alias not in stamp_cache:
        stamp_cache[target.alias] = _live_stamp(target.stamp_url)
    live = stamp_cache[target.alias]
    if not live:
        check.detail = "build green, live stamp unreadable"
        return check
    check.live_sha = live
    carried = graph.contains(check.sha, live)
    if carried:
        check.detail = f"build green, live site on {live}"
        return check
    if carried is None:
        check.detail = f"build green, live stamp {live} not checkable"
        return check
    check.state = BUILDING
    check.detail = f"build green but the live site still serves {live}"
    return check


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

CHECKS_TABLE = "vomeos_release_checks"
FIXES_TABLE = "vomeos_release_fixes"

store.register_table(
    CHECKS_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {CHECKS_TABLE} (
        task_id          VARCHAR NOT NULL,
        repo             VARCHAR NOT NULL,
        sha              VARCHAR NOT NULL,
        state            VARCHAR NOT NULL,
        detail           TEXT,
        run_id           VARCHAR,
        run_url          TEXT,
        live_sha         VARCHAR,
        first_checked_at TIMESTAMP NOT NULL DEFAULT NOW(),
        merged_at        TIMESTAMP,
        deployed_at      TIMESTAMP,
        last_checked_at  TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (task_id, repo, sha)
    )
    """,
)
store.register_table(
    FIXES_TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {FIXES_TABLE} (
        task_id          VARCHAR PRIMARY KEY,
        name             TEXT,
        fix_commit       TEXT,
        state            VARCHAR NOT NULL,
        alerted_state    VARCHAR,
        alerted_at       TIMESTAMP,
        dry_alerted_state VARCHAR,
        live_at          TIMESTAMP,
        last_checked_at  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)


class Ledger:
    """Postgres-backed memory of what was seen and what was said.

    `available` False means no database: the watcher still checks and still
    sets ClickUp fields, but cannot time out a stuck merge and posts only when
    a ClickUp field actually changed.
    """

    def __init__(self, enabled: bool = True):
        # `enabled` False is the print-only CLI: a local .env can point at the
        # production database, and a look must not create tables or rows.
        self.available = enabled and (
            store.ensure_table(CHECKS_TABLE)
            and store.ensure_table(FIXES_TABLE)
        )

    def record_pair(self, task_id: str, check: PairCheck) -> dict:
        """Upsert one pair, keeping first-seen times. Returns the row."""
        if not self.available:
            return {}
        now = _now()
        merged = check.state in (BUILDING, DEPLOYED, BUILD_FAILED)
        rows = store.query(
            f"INSERT INTO {CHECKS_TABLE} ("
            " task_id, repo, sha, state, detail, run_id, run_url, live_sha,"
            " first_checked_at, merged_at, deployed_at, last_checked_at"
            ") VALUES ("
            " :task_id, :repo, :sha, :state, :detail, :run_id, :run_url,"
            " :live_sha, :now,"
            " CASE WHEN :merged THEN CAST(:now AS TIMESTAMP) END,"
            " CASE WHEN :deployed THEN CAST(:now AS TIMESTAMP) END, :now"
            ") ON CONFLICT (task_id, repo, sha) DO UPDATE SET"
            " state = EXCLUDED.state,"
            " detail = EXCLUDED.detail,"
            " run_id = COALESCE(NULLIF(EXCLUDED.run_id, ''), "
            f"  {CHECKS_TABLE}.run_id),"
            " run_url = COALESCE(NULLIF(EXCLUDED.run_url, ''), "
            f"  {CHECKS_TABLE}.run_url),"
            " live_sha = COALESCE(NULLIF(EXCLUDED.live_sha, ''), "
            f"  {CHECKS_TABLE}.live_sha),"
            f" merged_at = COALESCE({CHECKS_TABLE}.merged_at,"
            "  EXCLUDED.merged_at),"
            f" deployed_at = COALESCE({CHECKS_TABLE}.deployed_at,"
            "  EXCLUDED.deployed_at),"
            " last_checked_at = EXCLUDED.last_checked_at"
            " RETURNING merged_at, deployed_at",
            {
                "task_id": task_id,
                "repo": check.alias,
                "sha": check.sha,
                "state": check.state,
                "detail": check.detail,
                "run_id": check.run_id,
                "run_url": check.run_url,
                "live_sha": check.live_sha,
                "merged": merged,
                "deployed": check.state == DEPLOYED,
                "now": now.replace(tzinfo=None),
            },
            write=True,
        )
        return rows[0] if rows else {}

    def last_alerted(self, task_id: str, dry_run: bool) -> str | None:
        if not self.available:
            return None
        column = "dry_alerted_state" if dry_run else "alerted_state"
        rows = store.query(
            f"SELECT {column} AS s FROM {FIXES_TABLE} WHERE task_id = :t",
            {"t": task_id},
        )
        return rows[0]["s"] if rows else None

    def record_fix(
        self,
        task_id: str,
        name: str,
        fix_commit: str,
        state: str,
        *,
        alerted: bool,
        dry_run: bool,
    ) -> None:
        if not self.available:
            return
        column = "dry_alerted_state" if dry_run else "alerted_state"
        now = _now().replace(tzinfo=None)
        store.execute(
            f"INSERT INTO {FIXES_TABLE} ("
            f" task_id, name, fix_commit, state, {column}, alerted_at,"
            " live_at, last_checked_at"
            ") VALUES ("
            " :task_id, :name, :fix_commit, :state,"
            " CASE WHEN :alerted THEN :state END,"
            " CASE WHEN :alerted THEN CAST(:now AS TIMESTAMP) END,"
            " CASE WHEN :live THEN CAST(:now AS TIMESTAMP) END, :now"
            ") ON CONFLICT (task_id) DO UPDATE SET"
            " name = EXCLUDED.name,"
            " fix_commit = EXCLUDED.fix_commit,"
            " state = EXCLUDED.state,"
            f" {column} = CASE WHEN :alerted THEN EXCLUDED.state"
            f"  ELSE {FIXES_TABLE}.{column} END,"
            " alerted_at = CASE WHEN :alerted THEN EXCLUDED.alerted_at"
            f"  ELSE {FIXES_TABLE}.alerted_at END,"
            f" live_at = COALESCE({FIXES_TABLE}.live_at, EXCLUDED.live_at),"
            " last_checked_at = EXCLUDED.last_checked_at",
            {
                "task_id": task_id,
                "name": name,
                "fix_commit": fix_commit,
                "state": state,
                "alerted": alerted,
                "live": state == FIX_LIVE and not dry_run,
                "now": now,
            },
        )


# ---------------------------------------------------------------------------
# ClickUp and Slack
# ---------------------------------------------------------------------------

def _clickup(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """One ClickUp call. Raises so the caller can report the failure."""
    resp = httpx.request(
        method,
        CLICKUP_BASE + path,
        headers={
            "Authorization": CLICKUP_API_TOKEN,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _list_on_dev_tasks() -> list[dict]:
    """Every Agent Fixes task at `on dev`, parents and children, all pages."""
    tasks: list[dict] = []
    for page in range(20):
        data = _clickup(
            f"/list/{AGENT_FIXES_LIST}/task"
            f"?statuses[]=on%20dev&subtasks=true&include_closed=false"
            f"&page={page}"
        )
        batch = data.get("tasks") or []
        tasks.extend(batch)
        if data.get("last_page", True) or not batch:
            break
    return tasks


def _field(task: dict, field_id: str) -> dict:
    for f in task.get("custom_fields") or []:
        if f.get("id") == field_id:
            return f
    return {}


def _release_check_value(task: dict) -> str:
    """"waiting" / "failed" / "live" / "" from the task's dropdown value.

    ClickUp returns a dropdown's value as the option's orderindex, and writes
    take the option id, so both directions go through the field's options.
    """
    f = _field(task, FIELD_RELEASE_CHECK)
    value = f.get("value")
    if value is None or value == "":
        return ""
    for option in (f.get("type_config") or {}).get("options") or []:
        if value in (option.get("orderindex"), option.get("id")):
            for name, option_id in RELEASE_CHECK_OPTIONS.items():
                if option.get("id") == option_id:
                    return name
    return ""


def _set_release_check(task_id: str, value: str) -> None:
    _clickup(
        f"/task/{task_id}/field/{FIELD_RELEASE_CHECK}",
        method="POST",
        body={"value": RELEASE_CHECK_OPTIONS[value]},
    )


def _post(text: str) -> bool:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        client.chat_postMessage(
            channel=SPRINTS_CHANNEL, text=text, unfurl_links=False,
        )
        return True
    except SlackApiError as exc:
        print(f"[RELEASE] Slack post failed: {exc.response['error']}")
        return False
    except Exception as exc:
        print(f"[RELEASE] Slack post failed: {exc}")
        return False


def _ticket_number(name: str) -> str:
    match = re.search(r"#(\d{3,6})", name or "")
    return match.group(1) if match else ""


def _children(parent_id: str, tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("parent") == parent_id]


def _client_line(child: dict) -> str:
    link = (_field(child, FIELD_ZOHO_TICKET_LINK).get("value") or "").strip()
    label = child.get("name") or child.get("id")
    number = _ticket_number(label)
    zoho = f" <{link}|Zoho #{number}>" if link and number else (
        f" <{link}|Zoho>" if link else " (no Zoho link)"
    )
    return f"  - <{child.get('url')}|{label}>{zoho}"


def _pair_line(check: PairCheck) -> str:
    run = f" <{check.run_url}|run {check.run_id}>" if check.run_url else ""
    return f"  - `{check.alias}@{check.sha}`: {check.detail}{run}"


def _message(
    state: str, task: dict, checks: list[PairCheck], children: list[dict],
    errors: list[str], dry_run: bool,
) -> str:
    title = f"<{task.get('url')}|{task.get('name')}>"
    prefix = "[dry run, nothing written] " if dry_run else ""
    lines: list[str] = []
    if state == FIX_LIVE:
        count = len(children)
        lines.append(f"{prefix}:white_check_mark: *Fix live:* {title}")
        lines += [_pair_line(c) for c in checks]
        if count:
            noun = "client" if count == 1 else "clients"
            lines.append(f"*{count} {noun} to notify:*")
            lines += [_client_line(c) for c in children]
            lines.append(
                "Resolution drafts come to Sam for approval. Nothing has been "
                "sent, and the tasks stay at on dev until the replies go out."
            )
        else:
            lines.append("No client tickets are linked, so nobody to notify.")
        lines.append(
            "Release Check would be set to Live." if dry_run
            else "Release Check set to Live."
        )
    elif state == FIX_FAILED:
        lines.append(f"{prefix}:x: *Release check failed:* {title}")
        lines += [_pair_line(c) for c in checks]
        lines.append(
            "Release Check would be set to Failed." if dry_run
            else "Release Check set to Failed. Clients have not been told "
            "anything."
        )
    elif state == FIX_WAITING:
        # Never posted, only printed by the on-demand run.
        lines.append(f"{prefix}:hourglass: *Waiting:* {title}")
        lines += [_pair_line(c) for c in checks]
    else:
        lines.append(f"{prefix}:grey_question: *Release check could not run:* "
                     f"{title}")
        lines += [f"  - {e}" for e in errors]
        lines += [_pair_line(c) for c in checks if c.state == UNKNOWN]
        lines.append("Left as it is. A failed check is not a failed release.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def _fix_state(checks: list[PairCheck], errors: list[str]) -> str:
    if errors:
        return FIX_UNKNOWN
    states = {c.state for c in checks}
    if BUILD_FAILED in states:
        return FIX_FAILED
    if UNKNOWN in states:
        return FIX_UNKNOWN
    if checks and states == {DEPLOYED}:
        return FIX_LIVE
    return FIX_WAITING


def evaluate_task(
    task: dict,
    ledger: Ledger,
    *,
    github: GitHub,
    bitbucket: Bitbucket,
    graphs: dict,
    runs_cache: dict,
    stamp_cache: dict,
) -> tuple[str, list[PairCheck], list[str]]:
    """Check every commit of one parent and decide the fix state."""
    text = (_field(task, FIELD_FIX_COMMIT).get("value") or "").strip()
    pairs, errors = parse_fix_commit(text)
    if not pairs and not errors:
        errors = ["Fix Commit is empty"]
    checks: list[PairCheck] = []
    for alias, sha in pairs:
        check = check_pair(
            alias, sha,
            github=github, bitbucket=bitbucket, graphs=graphs,
            runs_cache=runs_cache, stamp_cache=stamp_cache,
        )
        row = ledger.record_pair(task["id"], check)
        merged_at = row.get("merged_at")
        if check.state == BUILDING and merged_at is not None:
            if merged_at.tzinfo is None:
                merged_at = merged_at.replace(tzinfo=timezone.utc)
            waited = _now() - merged_at
            if waited > _timeout():
                minutes = int(waited.total_seconds() // 60)
                check.state = BUILD_FAILED
                check.detail = (
                    f"merged {minutes // 60}h {minutes % 60}m ago and still "
                    f"not deployed ({check.detail})"
                )
        checks.append(check)
    return _fix_state(checks, errors), checks, errors


def run_release_watcher(
    dry_run: bool = False,
    *,
    post: bool = True,
    task_id: str = "",
) -> dict:
    """One pass over the Agent Fixes list. Safe to run as often as you like.

    Live by default: the scheduled job sets Release Check and posts.
    `dry_run=True` is the on-demand CLI's print-only mode, never the job's.
    """
    if not _enabled():
        print("[RELEASE] RELEASE_WATCHER_ENABLED is false, skipping")
        return {"status": "disabled"}
    if not CLICKUP_API_TOKEN:
        print("[RELEASE] CLICKUP_API_TOKEN not set")
        return {"status": "unconfigured"}

    if post and not dry_run:
        # Agent-opened fix PRs that have merged move to on dev first, so
        # this same pass can start watching them.
        try:
            import ticket_fix

            ticket_fix.run_promotion()
        except Exception as exc:
            print(f"[RELEASE] fix PR promotion failed: {exc}")

    try:
        tasks = _list_on_dev_tasks()
    except Exception as exc:
        # An unreadable list is not an empty one.
        print(f"[RELEASE] could not list Agent Fixes: {exc}")
        return {"status": "unreachable", "error": str(exc)}

    parents = [
        t for t in tasks
        if not t.get("parent")
        and (_field(t, FIELD_FIX_COMMIT).get("value") or "").strip()
        and (not task_id or t.get("id") == task_id)
    ]

    ledger = Ledger() if post else Ledger(enabled=False)
    github, bitbucket = GitHub(), Bitbucket()
    graphs: dict = {}
    runs_cache: dict = {}
    stamp_cache: dict = {}
    results = []

    for task in parents:
        state, checks, errors = evaluate_task(
            task, ledger,
            github=github, bitbucket=bitbucket, graphs=graphs,
            runs_cache=runs_cache, stamp_cache=stamp_cache,
        )
        children = _children(task["id"], tasks)

        # ClickUp. Only Live and Failed are written, plus Waiting when a
        # Failed fix has recovered into a fresh build. Unknown writes nothing.
        current = _release_check_value(task)
        wanted = {FIX_LIVE: "live", FIX_FAILED: "failed"}.get(state)
        if state == FIX_WAITING and current == "failed":
            wanted = "waiting"
        field_changed = False
        if wanted and wanted != current and not dry_run:
            try:
                _set_release_check(task["id"], wanted)
                field_changed = True
            except Exception as exc:
                print(f"[RELEASE] could not set Release Check on "
                      f"{task['id']}: {exc}")

        # Slack. Only on a change of state, and never for plain waiting.
        # `alerted` means "remember this state as told": a post that landed,
        # or a quiet move back to waiting, which re-arms the next Live or
        # Failed. A Slack failure leaves it False, so the next pass retries.
        if ledger.available:
            changed = ledger.last_alerted(task["id"], dry_run) != state
        else:
            changed = field_changed
        alerted = False
        if not post:
            print(_message(state, task, checks, children, errors, dry_run))
        elif state == FIX_WAITING:
            alerted = changed
        elif changed:
            alerted = _post(
                _message(state, task, checks, children, errors, dry_run)
            )

        if post:
            ledger.record_fix(
                task["id"], task.get("name") or "",
                (_field(task, FIELD_FIX_COMMIT).get("value") or ""),
                state, alerted=alerted, dry_run=dry_run,
            )

        results.append({
            "task_id": task["id"],
            "name": task.get("name"),
            "state": state,
            "release_check": current,
            "set_to": wanted if field_changed else "",
            "alerted": alerted,
            "children": [c["id"] for c in children],
            "pairs": [
                {"repo": c.alias, "sha": c.sha, "state": c.state,
                 "detail": c.detail, "run": c.run_url}
                for c in checks
            ],
            "errors": errors,
        })
        print(f"[RELEASE] {task['id']} {state}: "
              + "; ".join(f"{c.alias}@{c.sha} {c.state}" for c in checks))

    return {
        "status": "ok",
        "dry_run": dry_run,
        "ledger": ledger.available,
        "fixes": results,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--task", default="", help="one parent task id")
    parser.add_argument(
        "--live", action="store_true",
        help="post to Slack and write ClickUp (default: print only)",
    )
    args = parser.parse_args()
    summary = run_release_watcher(
        dry_run=not args.live, post=args.live, task_id=args.task,
    )
    print(json.dumps(summary, indent=2, default=str))
