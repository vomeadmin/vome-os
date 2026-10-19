"""
feature_desk.py

#feature-requests, end to end. A teammate posts a request (usually Ron, a
backend handoff written with Claude from his frontend work, with a ClickUp
spec linked; sometimes demo notes or a client ask). The agent works out what
the backend should really do and answers in the thread, item by item:
built by an agent (a pull request), a shaping question for Sam, logged for
Sam, already there, frontend only, or a question for the requester.

WHY THIS EXISTS
---------------
Sam, 2026-10-19: the frontend team asks for the right thing from the
frontend's side, but not always the right backend shape. A new form setting
can really be a new step type, or one more option inside a micro-setting
that already exists. Building whatever is asked at that moment, every time,
makes the backend messy. So an architect reads each request against the
code and the established extension patterns before anything is built, and
only the plainly additive work (a field on a response, a field on an update,
a bulk version of an existing write) goes straight to a pull request.

FLOW
----
    top-level message (web)  ->  "Analysing this." in the thread
                             ->  enqueue engineering.feature_request -> worker:
      1. the post, its thread, the ClickUp task it links, existing tasks
      2. engineering.feature_architect reads the code (production and the
         dev branches) and returns one verdict per request in the post
      3. agent_build    -> engineering.fix_author writes the diff, patch
                           safety, PR into the dev branch, Agent Fixes task
                           (a build that falls through is logged for Sam)
         decision       -> options + a recommendation, Sam is asked
         log            -> the linked task gets the analysis (it is already
                           in Sam's queue); with no task, Sam says `log N`
         already_exists, frontend_only -> the finding, nothing built
         needs_info     -> the requester is asked one question

Replies in the thread, from Sam: `go N` / `go NB` (build item N, or option
B of it), `log N`, `no N`, `close`. Any other reply from Sam is direction:
the architect runs again with it and the items still open are re-decided.
A reply from anyone else re-runs the analysis only while an item is waiting
on information.

SWITCHES
--------
    FEATURE_DESK_ENABLED          default OFF. "true" listens to the channel.
    FEATURE_DESK_PR_ENABLED       default OFF. "true" opens pull requests;
                                  off, an agent_build item waits for `go N`
                                  and is refused with the reason.
    FEATURE_DESK_PR_DAILY_CAP     pull requests per day, default 3. Sam's
                                  `go N` is not counted against it.
    SLACK_CHANNEL_FEATURE_REQUESTS default C0ALL4VPWK0 (shared with
                                  sprint_thread's feature posts).

Pull requests also need the repo in VOMEOS_CODE_WRITE_REPOS and
VOMEOS_PR_ALLOW_PROTECTED_TARGET=true, exactly like the other fix paths.

Dry run over the channel's history, nothing posted or written:
`py feature_desk.py --backfill --dry-run --limit 10`.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from vomeos import store

CHANNEL = os.environ.get("SLACK_CHANNEL_FEATURE_REQUESTS", "C0ALL4VPWK0")
HANDLER_KEY = "engineering.feature_request"
QUEUE = "engineering"
AGENT = "engineering.feature_architect"

SAM_SLACK = "U01B9FQNSBU"
SAM_CLICKUP = 3691763
RAW_INTAKE = "901113386484"
FIELD_SOURCE = "857e1262-cb5c-4c22-b8da-770a8fcfa82e"
SOURCE_SLACK = "e62588d7-4b72-45a8-a9ab-a2c20a860903"

# Only the backend repositories with an integration branch. Frontend work is
# the requester's own, and a repo with no dev branch has nowhere safe to PR.
BUILD_REPOS = ("core", "database")

# A post shorter than this is chat ("Amazing", "+1"), not a request.
MIN_REQUEST_CHARS = 40
MAX_ROUNDS = 4
MAX_ITEMS = 6

_TASK_LINK = re.compile(r"app\.clickup\.com/t/(?:\d+/)?([a-z0-9]+)")
_COMMAND = re.compile(r"^(go|log|no)\b\s*(.*)$", re.I)
_TARGET = re.compile(r"(\d+)\s*([a-z])?\b", re.I)
# A thank-you is not direction, and must not cost a re-analysis.
_CHAT = re.compile(r"^(thanks?( you)?|ty|ok(ay)?|great|nice|cool|perfect|"
                   r"got it|sounds good|:[a-z0-9_+-]+:)\W*$", re.I)

# Item states. FINAL items are never re-decided by a later analysis.
BUILDING, PR_OPEN, WAITING_SAM, NEEDS_INFO = (
    "building", "pr_open", "waiting_sam", "needs_info")
LOGGED, DECLINED, EXISTS, FRONTEND, HELD = (
    "logged", "declined", "exists", "frontend", "held")
FINAL = (PR_OPEN, LOGGED, DECLINED, EXISTS, FRONTEND)

# Post states.
ANALYSING, OPEN, CLOSED, ERROR = "analysing", "open", "closed", "error"

VERDICT_LABEL = {
    "agent_build": "Agent builds it",
    "decision": "Needs your call",
    "log": "Logged for Sam",
    "already_exists": "Already there",
    "frontend_only": "Frontend only",
    "needs_info": "Question",
}

LEDGER = "vomeos_feature_desk"
store.register_table(
    LEDGER,
    f"""
    CREATE TABLE IF NOT EXISTS {LEDGER} (
        ts            VARCHAR PRIMARY KEY,
        requester     VARCHAR,
        task_id       VARCHAR,
        status        VARCHAR,
        card          TEXT,
        items         TEXT,
        rounds        INTEGER NOT NULL DEFAULT 0,
        last_reply_ts VARCHAR,
        created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def enabled() -> bool:
    return _on("FEATURE_DESK_ENABLED")


def pr_enabled() -> bool:
    return _on("FEATURE_DESK_PR_ENABLED")


def daily_cap() -> int:
    return int(os.environ.get("FEATURE_DESK_PR_DAILY_CAP", "3"))


# ---------------------------------------------------------------------------
# Web side: the Slack event
# ---------------------------------------------------------------------------

def handle_message(event: dict) -> None:
    """One human message in #feature-requests. Cheap, never raises."""
    if not enabled():
        return
    ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    text = str(event.get("text") or "").strip()
    if not ts or not text:
        return
    try:
        from vomeos.worker import enqueue_event

        if not thread_ts or thread_ts == ts:
            # A bare ClickUp link is a request too: it is how Sam sends a
            # Priority Queue handoff that never had a post of its own.
            if len(text) < MIN_REQUEST_CHARS and not _TASK_LINK.search(text):
                return
            _post("Analysing this. I'll check it against the backend and "
                  "reply here with what can be built and what needs Sam.",
                  thread_ts=ts)
            enqueue_event(HANDLER_KEY, {"ts": ts}, queue=QUEUE)
        else:
            enqueue_event(HANDLER_KEY, {
                "ts": thread_ts,
                "reply": {"ts": ts, "user": event.get("user") or "",
                          "text": text},
            }, queue=QUEUE)
    except Exception as exc:
        print(f"[FEATURE DESK] could not queue {ts}: {exc}")


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

def handle(message: dict) -> dict:
    """One request post, or one reply in its thread. Never raises."""
    ts = str(message.get("ts") or "")
    if not ts or not enabled():
        return {"status": "skipped", "reason": "disabled or no message"}
    if not store.ensure_table(LEDGER):
        return {"status": "skipped", "reason": "no database"}
    try:
        reply = message.get("reply")
        if reply:
            return _on_reply(ts, reply)
        row = _row(ts)
        if row and row.get("status") != ERROR:
            return {"status": "skipped", "reason": "already handled"}
        _save(ts, status=ANALYSING)
        return analyse(ts)
    except Exception as exc:
        print(f"[FEATURE DESK] {ts} failed: {type(exc).__name__}: {exc}")
        _save(ts, status=ERROR)
        _post(f"I couldn't finish analysing this ({type(exc).__name__}). "
              f"<@{SAM_SLACK}> it needs a look.", thread_ts=ts)
        return {"status": "error", "error": str(exc)}


def _on_reply(ts: str, reply: dict) -> dict:
    row = _row(ts)
    if not row:
        return {"status": "skipped", "reason": "not a request we handled"}
    if row.get("last_reply_ts") == reply.get("ts"):
        return {"status": "skipped", "reason": "reply already handled"}
    _save(ts, last_reply_ts=reply.get("ts"))
    text = str(reply.get("text") or "").strip()
    items = _items(row)

    if reply.get("user") == SAM_SLACK:
        if text.lower().rstrip(".! ") == "close":
            _save(ts, status=CLOSED)
            _post("Closed. Nothing more will be built or filed from this "
                  "thread.", thread_ts=ts)
            return {"status": "ok", "action": "closed"}
        command = parse_command(text, items)
        if command:
            return _command(ts, row, items, command)
        if _CHAT.match(text):
            return {"status": "skipped", "reason": "acknowledgement"}
        # Free text from Sam is direction on how to shape it.
        return analyse(ts, direction=text)

    if any(i.get("state") == NEEDS_INFO for i in items.values()):
        return analyse(ts)
    return {"status": "skipped", "reason": "chat, nothing waiting on it"}


def parse_command(text: str, items: dict) -> dict | None:
    """`go 2`, `go 2B`, `go 1 3`, `log 3`, `no 4`, or a bare verb meaning
    every open item. None when the message is not exactly a command, so
    "go with B but keep it on the role" reads as direction, not as `go`."""
    match = _COMMAND.match(text.strip().strip("`*_").rstrip(".!"))
    if not match:
        return None
    verb, rest = match.group(1).lower(), match.group(2).strip()
    targets = []
    if rest:
        cleaned = re.sub(r"\b(and|item|items)\b|[,&]", " ", rest, flags=re.I)
        if _TARGET.sub("", cleaned).strip():
            return None
        targets = [(n, (opt or "").upper())
                   for n, opt in _TARGET.findall(cleaned)]
    else:
        targets = [(n, "") for n, item in items.items()
                   if item.get("state") not in FINAL]
    if not targets:
        return None
    return {"verb": verb, "targets": targets}


def _command(ts: str, row: dict, items: dict, command: dict) -> dict:
    verb = command["verb"]
    unknown = [n for n, _ in command["targets"] if n not in items]
    if unknown:
        _post(f"There's no item {', '.join(unknown)} on this request.",
              thread_ts=ts)
        return {"status": "ok", "action": "unknown item"}

    if verb == "no":
        for n, _ in command["targets"]:
            items[n]["state"] = DECLINED
        _save(ts, items=json.dumps(items))
        _post(f"Not building {_list(command['targets'])}. Nothing filed.",
              thread_ts=ts)
        return {"status": "ok", "action": "declined"}

    if verb == "log":
        lines = [_log(ts, row, items, n, sam_said=True)
                 for n, _ in command["targets"]]
        _save(ts, items=json.dumps(items))
        _post("\n".join(lines), thread_ts=ts)
        return {"status": "ok", "action": "logged"}

    # go: an item already planned for an agent builds now; anything else
    # is re-decided with Sam's choice as direction, because the build plan
    # for the option he picked has not been written yet.
    ready = [n for n, opt in command["targets"]
             if not opt and items[n].get("verdict") == "agent_build"
             and items[n].get("state") in (HELD, WAITING_SAM)]
    rest = [(n, opt) for n, opt in command["targets"] if n not in ready]
    results = [_build(ts, row, items, n, sam_approved=True) for n in ready]
    if rest:
        direction = "; ".join(
            f"Sam: build item {n}" + (f" using option {opt}" if opt else
                                      " using your recommendation")
            for n, opt in rest)
        return analyse(ts, direction=direction,
                       approved=[n for n, _ in rest])
    return {"status": "ok", "action": "built", "results": results}


def analyse(ts: str, direction: str = "", approved: list | None = None,
            dry_run: bool = False, post: dict | None = None) -> dict:
    """Run the architect on the post and act on each item's verdict.
    `post` stands in for the Slack message when a ClickUp task is
    analysed without one (a dry run of the Priority Queue)."""
    from vomeos import run_agent

    post = post or _read_post(ts)
    if not post:
        return {"status": "skipped", "reason": "message unreadable"}
    row = {} if dry_run else _row(ts)
    rounds = int(row.get("rounds") or 0) + 1
    previous = _items(row)
    task_id = post["task_id"]
    context = {
        "request": post["text"],
        "requester": post["requester"],
        "thread": post["thread"] or "(no replies)",
        "linked_task": _task_text(task_id),
        "existing_tasks": _existing(post["text"]),
        "previous_items": _previous_text(previous),
        "sam_direction": direction or "(none)",
    }
    result = run_agent(AGENT, context=context,
                       subject_type="slack_feature_request", subject_id=ts)
    if not result.ok:
        card = {"summary": "The analysis did not finish", "items": [{
            "n": 1, "title": "Request", "verdict": "decision",
            "analysis": f"The architect returned nothing usable "
                        f"({result.why()}).",
            "question": "Can you take a look at this one?"}]}
    else:
        card = result.data or {}

    items = _merge(previous, card.get("items") or [], set(approved or []))
    if not dry_run:
        _save(ts, requester=post["requester"], task_id=task_id,
              card=json.dumps(card), items=json.dumps(items),
              rounds=rounds, status=OPEN)
    row = {**row, "ts": ts, "task_id": task_id,
           "requester": post["requester"],
           "requester_id": post["requester_id"], "rounds": rounds}
    return act(ts, row, card, items, dry_run=dry_run)


def _merge(previous: dict, fresh: list, approved: set) -> dict:
    """The architect's new items over the old ones. A FINAL item keeps its
    state whatever the new run says, so a re-analysis can never reopen a
    pull request or un-log a request."""
    items = dict(previous)
    for raw in fresh[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        n = str(raw.get("n") or len(items) + 1)
        old = items.get(n) or {}
        if old.get("state") in FINAL:
            continue
        items[n] = {**raw, "n": n, "state": "",
                    "sam_approved": n in approved or
                    bool(old.get("sam_approved"))}
    return items


def act(ts: str, row: dict, card: dict, items: dict,
        dry_run: bool = False) -> dict:
    """One thread reply with every item, then the builds, each reporting
    back in the thread when its pull request opens or falls through."""
    if card.get("not_a_request") and not any(
            i.get("state") in FINAL for i in items.values()):
        text = f"No backend request here, so nothing to do. " \
               f"{card.get('summary') or ''}".strip()
        if dry_run:
            return _dry(ts, card, items, [f"thread: {text}"])
        _post(text, thread_ts=ts)
        _save(ts, status=CLOSED)
        return {"status": "ok", "verdict": "not_a_request"}

    over_rounds = int(row.get("rounds") or 0) >= MAX_ROUNDS
    to_build, lines, asks_sam = [], [], False
    for n, item in sorted(items.items(), key=lambda kv: int(kv[0])):
        if item.get("state") in FINAL:
            continue
        verdict = str(item.get("verdict") or "decision")
        if verdict == "needs_info" and over_rounds:
            verdict = item["verdict"] = "decision"
            item["question"] = (f"I've asked for more information "
                                f"{MAX_ROUNDS} times. Can you take it?")
        if verdict == "agent_build":
            ok, why = _can_build(item)
            if ok:
                item["state"] = BUILDING
                to_build.append(n)
            else:
                item["verdict"], item["state"] = "log", ""
                item["analysis"] = (f"{item.get('analysis') or ''} "
                                    f"Not built by an agent: {why}.").strip()
                verdict = "log"
        if verdict == "decision":
            item["state"] = WAITING_SAM
            asks_sam = True
        elif verdict == "needs_info":
            item["state"] = NEEDS_INFO
        elif verdict == "already_exists":
            item["state"] = EXISTS
        elif verdict == "frontend_only":
            item["state"] = FRONTEND
        elif verdict == "log":
            if row.get("task_id"):
                item["state"] = LOGGED
            else:
                item["state"] = WAITING_SAM
                asks_sam = True
        lines.append(_item_text(item, row))

    if not lines:
        if not dry_run:
            _post("Nothing is still open on this request.", thread_ts=ts)
        return {"status": "skipped", "reason": "nothing open"}

    header = f"*{card.get('summary') or 'Request'}*"
    footer = []
    if asks_sam:
        footer.append(f"<@{SAM_SLACK}> reply `go N` (or `go NB` for "
                      "option B), `log N`, `no N`, or tell me how you'd "
                      "shape it.")
    if any(i.get("state") == NEEDS_INFO for i in items.values()):
        who = row.get("requester_id") or ""
        footer.append(f"{f'<@{who}> ' if who else ''}an answer here and "
                      "I'll look again.")
    text = "\n\n".join([header, *lines, *footer])

    if dry_run:
        plans = [f"build {n}: {items[n].get('build')}" for n in to_build]
        return _dry(ts, card, items, [f"thread:\n{text}", *plans])

    _post(text, thread_ts=ts)
    if row.get("task_id"):
        _comment(row["task_id"], _comment_text(card, items, ts))
        _set_source(row["task_id"])
    _save(ts, items=json.dumps(items))

    results = [_build(ts, row, items, n) for n in to_build]
    return {"status": "ok", "items": {n: i.get("verdict")
                                      for n, i in items.items()},
            "builds": results}


def _item_text(item: dict, row: dict) -> str:
    verdict = str(item.get("verdict") or "")
    label = VERDICT_LABEL.get(verdict, verdict)
    lines = [f"*{item['n']}. {item.get('title') or 'Request'}* · {label}"]
    if item.get("shape"):
        lines.append(f"_Shape:_ {item['shape']}")
    lines.append(str(item.get("analysis") or ""))
    if item.get("evidence"):
        lines.append(f"_Where:_ `{item['evidence']}`")
    for option in item.get("options") or []:
        if not isinstance(option, dict):
            continue
        tag = " (agent can build)" if option.get("agent_can_build") else ""
        lines.append(f"*{option.get('key')}.* {option.get('shape')}{tag}. "
                     f"{option.get('tradeoffs') or ''}".strip())
    if item.get("recommendation"):
        lines.append(f"_Recommend:_ {item['recommendation']}")
    if item.get("question"):
        lines.append(f"_Question:_ {item['question']}")
    if item.get("state") == BUILDING:
        lines.append("Building it now. The pull request link follows here.")
    if verdict == "log" and item.get("state") == LOGGED:
        lines.append("Added to the linked ClickUp task for Sam.")
    elif verdict == "log":
        lines.append(f"`log {item['n']}` adds it to Raw Intake.")
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _can_build(item: dict) -> tuple[bool, str]:
    """The deterministic half of the envelope. The architect judges the
    shape; this checks the plan is concrete enough to write and lands in a
    repo with a dev branch. Sam's word lifts the daily cap, never these.
    Whether pull requests are switched on is decided at build time, so the
    plan still shows in the thread while they are off."""
    build = item.get("build") or {}
    if not isinstance(build, dict) or not build.get("plan"):
        return False, "no build plan"
    if str(build.get("repo") or "") not in BUILD_REPOS:
        return False, (f"repo {build.get('repo')!r} is not one an agent "
                       "builds in (core, database)")
    anchors = [a for a in build.get("anchors") or [] if isinstance(a, str)]
    if not anchors or len(anchors) > 3:
        return False, "needs one to three exact file:line anchors"
    if not all(re.match(r"^[\w./-]+\.py:\d+$", a) for a in anchors):
        return False, f"anchors are not exact path:line ({anchors})"
    return True, ""


def _build(ts: str, row: dict, items: dict, n: str,
           sam_approved: bool = False) -> dict:
    """fix_author writes the diff, patch safety checks it, a PR opens into
    the dev branch. Anything that stops it is said in the thread, and the
    item goes to Sam rather than disappearing."""
    item = items[n]
    sam_approved = sam_approved or bool(item.get("sam_approved"))
    ok, why = _can_build(item)
    if ok and not pr_enabled():
        ok, why = False, "pull requests are switched off " \
                         "(FEATURE_DESK_PR_ENABLED)"
    if ok and not sam_approved and _opened_today() >= daily_cap():
        ok, why = False, f"the daily cap of {daily_cap()} pull requests " \
                         "is reached"
    if not ok:
        item["state"] = HELD
        _save(ts, items=json.dumps(items))
        _post(f"*{n}* is ready to build, but {why}. Sam can say "
              f"`go {n}` once that's cleared, or `log {n}`.", thread_ts=ts)
        return {"n": n, "built": False, "reason": why}

    proposal, repo = _propose(ts, item)
    if not proposal.get("diff"):
        reason = proposal.get("reason") or "no diff"
        return _fell_through(ts, row, items, n,
                             f"the builder declined: {reason}")
    pr = _open_pr(ts, row, item, proposal, repo)
    if not pr.get("opened"):
        return _fell_through(ts, row, items, n,
                             f"the PR did not open: {pr.get('reason')}")

    fix_task = _file_fix_task(item, repo, pr["url"], ts, row)
    task_url = f"https://app.clickup.com/t/{fix_task}" if fix_task else ""
    item.update(state=PR_OPEN, pr_url=pr["url"], fix_task=fix_task,
                pr_day=_today(), sam_approved=sam_approved)
    _save(ts, items=json.dumps(items))
    if row.get("task_id"):
        _comment(row["task_id"], f"Item {n} built by an agent: {pr['url']}\n"
                                 f"Agent Fixes task: {task_url}")
        _link(row["task_id"], fix_task)
    build = item.get("build") or {}
    _post("\n".join(line for line in (
        f"*{n}. Pull request open:* <{pr['url']}|into "
        f"{repo.alias} development>. Sam reviews and merges.",
        f"_What the frontend gets:_ {build.get('contract')}"
        if build.get("contract") else "",
        f"_Unsure about:_ {proposal.get('risks')}"
        if proposal.get("risks") else "",
        f"ClickUp: <{task_url}|Agent Fixes task>" if task_url else "",
    ) if line), thread_ts=ts)
    return {"n": n, "built": True, "pr_url": pr["url"]}


def _fell_through(ts: str, row: dict, items: dict, n: str,
                  why: str) -> dict:
    item = items[n]
    item["analysis"] = f"{item.get('analysis') or ''} Build stopped: " \
                       f"{why}.".strip()
    line = _log(ts, row, items, n, sam_said=False)
    _save(ts, items=json.dumps(items))
    _post(f"*{n}* didn't build ({why}). {line}", thread_ts=ts)
    return {"n": n, "built": False, "reason": why}


def _propose(ts: str, item: dict):
    """The fix author's diff for the plan, against the dev branch it will
    merge into. The anchors become a synthetic trace so the same source
    reader that serves Sentry fixes reads every file the plan touches."""
    import sentry_code
    import sentry_fix
    import sentry_projects
    from vomeos.tools import repo_map

    build = item.get("build") or {}
    repo = repo_map().get(str(build.get("repo") or ""))
    if not repo:
        return {"reason": f"unknown repo {build.get('repo')!r}"}, repo
    ref = sentry_projects.fix_target(repo.slug) or repo.branch
    anchors = [a.rsplit(":", 1) for a in build.get("anchors") or []]
    trace = "\n".join(f'File "{path}", line {line}, in feature_request'
                      for path, line in anchors)
    gathered = sentry_code.gather(trace, repo.slug, ref, host=repo.host,
                                  owner=repo.owner)
    proposal = sentry_fix.propose({
        "root_cause": (f"Not a bug. A requested backend change: "
                       f"{item.get('title')}. {item.get('analysis') or ''}"),
        "proposed_fix": (f"{build.get('plan')}\n\nWhat the frontend must "
                         f"receive: {build.get('contract') or 'see plan'}\n"
                         f"Follow this precedent: "
                         f"{item.get('evidence') or 'the code around it'}"),
        "risk": "low",
        "confidence": "high",
        "needs_migration": False,
        "files": [path for path, _ in anchors],
    }, {"source": gathered.get("files", ""), "repo": repo.slug,
        "branch": ref, "issue_id": _issue_key(ts)})
    return proposal, repo


def _open_pr(ts: str, row: dict, item: dict, proposal: dict, repo) -> dict:
    """Branch, commit, PR into the dev branch. The same apply-and-verify
    step as sentry_fix.open_pull_request, with a body that describes a
    requested change rather than a bug."""
    import sentry_fix
    import sentry_projects
    from vomeos.integrations import code_write

    ref = sentry_projects.fix_target(repo.slug)
    if not ref:
        return {"opened": False, "reason": f"no dev branch for {repo.slug}"}
    contents, error = sentry_fix._apply_to_repo(
        repo.slug, ref, repo.host, repo.owner, proposal["diff"])
    if error:
        return {"opened": False, "reason": f"patch does not apply: {error}"}

    writer = code_write.writer_for(repo.host)
    kwargs = {"owner": repo.owner} if repo.host == "github" and repo.owner \
        else {}
    if not writer.configured(**kwargs):
        return {"opened": False, "reason": "no write token configured"}

    title = str(item.get("title") or "Requested backend change")
    branch = code_write.branch_name(f"feature-{_issue_key(ts)}-{item['n']}",
                                    title[:40])
    message = f"Feature request: {title}"[:100]
    try:
        created = writer.create_branch(repo.slug, branch, ref, **kwargs)
        if not created.ok:
            return {"opened": False,
                    "reason": f"could not branch: {created.error[:140]}"}
        for path, body in contents.items():
            written = writer.commit_file(repo.slug, branch, path, body,
                                         message, **kwargs)
            if not written.ok:
                return {"opened": False, "reason": f"could not commit "
                                                   f"{path}: "
                                                   f"{written.error[:120]}"}
        opened = writer.open_pull_request(
            repo.slug, branch, ref, title=message,
            body=_pr_body(ts, row, item, proposal), **kwargs)
    except code_write.WriteDenied as exc:
        return {"opened": False, "reason": f"refused: {exc}"}
    if not opened.ok:
        return {"opened": False,
                "reason": f"could not open PR: {opened.error[:140]}"}
    return {"opened": True, "url": sentry_fix._pr_url(opened.data),
            "branch": branch}


def _pr_body(ts: str, row: dict, item: dict, proposal: dict) -> str:
    build = item.get("build") or {}
    link = _permalink(ts)
    return "\n".join([
        "Built automatically from a #feature-requests post. **Not reviewed "
        "by a person.** The architect judged it an additive change that "
        "follows an existing pattern; check that it does.",
        "",
        f"**Requested by** {row.get('requester') or 'a teammate'}"
        + (f" · [Slack]({link})" if link else "")
        + (f" · [ClickUp](https://app.clickup.com/t/{row['task_id']})"
           if row.get("task_id") else ""),
        "",
        "### What was asked",
        str(item.get("title") or ""),
        "",
        "### Why this shape",
        str(item.get("analysis") or ""),
        f"Precedent: `{item.get('evidence')}`" if item.get("evidence")
        else "",
        "",
        "### What the frontend gets",
        str(build.get("contract") or "see the diff"),
        "",
        "### What the author was unsure about",
        str(proposal.get("risks") or "none identified"),
        "",
        "---",
        "No migration, no new model, no auth, permission or billing code: "
        "the patch safety check refuses all of them.",
    ])


def _file_fix_task(item: dict, repo, pr_url: str, ts: str,
                   row: dict) -> str:
    """The Agent Fixes parent. Its "Fix PR:" line is what
    ticket_fix.promote_merged follows to `on dev` once the PR merges, and
    the release watcher proves it live from there."""
    import ticket_fix as tf

    build = item.get("build") or {}
    description = "\n".join([
        "**Requested backend change built by an agent from "
        "#feature-requests.** Not reviewed by a person. Merge into the dev "
        "branch only after review.",
        "",
        f"Fix PR: {pr_url}",
        f"**Repo:** {repo.alias} ({repo.slug}), into the dev branch",
        f"**Request:** {_permalink(ts)}",
        f"**Requester's task:** https://app.clickup.com/t/{row['task_id']}"
        if row.get("task_id") else "",
        "",
        f"**Shape:** {item.get('shape') or ''}",
        f"**Plan:** {build.get('plan') or ''}",
        f"**Contract:** {build.get('contract') or ''}",
        f"**Precedent:** `{item.get('evidence') or ''}`",
    ])
    try:
        task = _clickup("POST", f"/list/{tf.AGENT_FIXES_LIST}/task", {
            "name": f"[Feature request] {item.get('title') or ''}"[:180],
            "markdown_description": description,
            "status": "in progress",
            "custom_fields": [
                {"id": tf.FIELD_TAGS, "value": [tf.TAG_AGENT]},
                {"id": tf.FIELD_MERGE_OWNER, "value": {"add": [SAM_CLICKUP]}},
                {"id": tf.FIELD_RELEASE_CHECK, "value": tf.RELEASE_WAITING},
                {"id": FIELD_SOURCE, "value": SOURCE_SLACK},
            ],
        })
        return str(task.get("id") or "")
    except Exception as exc:
        print(f"[FEATURE DESK] Agent Fixes task not filed for {ts}: {exc}")
        return ""


def _opened_today() -> int:
    """Pull requests the agent opened on its own today. Counted in Python
    because the PR urls live inside each post's items JSON; `pr_day` keeps
    an older PR on a post touched today out of the count."""
    rows = store.query(
        f"SELECT items FROM {LEDGER}"
        " WHERE updated_at >= date_trunc('day', NOW())"
    )
    today = _today()
    return sum(
        1 for r in rows or [] for item in _items(dict(r)).values()
        if item.get("pr_day") == today and not item.get("sam_approved")
    )


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Logging for Sam
# ---------------------------------------------------------------------------

def _log(ts: str, row: dict, items: dict, n: str, sam_said: bool) -> str:
    """The linked task gets the analysis (it is already in Sam's queue).
    With no linked task, a Raw Intake task, but only on Sam's word: a
    client ask is his to prioritise, and an unasked task is noise."""
    item = items[n]
    task_id = row.get("task_id") or ""
    if task_id:
        _comment(task_id, _item_comment(item, ts))
        item["state"] = LOGGED
        return f"*{n}* logged on the linked ClickUp task."
    if not sam_said:
        item["state"] = WAITING_SAM
        return f"<@{SAM_SLACK}> `log {n}` adds it to Raw Intake."
    created = _create_task(item, ts, row)
    item["state"] = LOGGED
    item["clickup"] = created
    if not created:
        return f"*{n}*: the Raw Intake task could not be created. " \
               "Logged here only."
    return (f"*{n}* logged in Raw Intake: "
            f"<https://app.clickup.com/t/{created}|task>.")


def _create_task(item: dict, ts: str, row: dict) -> str:
    try:
        task = _clickup("POST", f"/list/{RAW_INTAKE}/task", {
            "name": str(item.get("title") or "Feature request")[:180],
            "markdown_description": _item_comment(item, ts),
            "custom_fields": [{"id": FIELD_SOURCE, "value": SOURCE_SLACK}],
        })
        return str(task.get("id") or "")
    except Exception as exc:
        print(f"[FEATURE DESK] Raw Intake task not created for {ts}: {exc}")
        return ""


def _item_comment(item: dict, ts: str) -> str:
    build = item.get("build") or {}
    lines = [
        f"**Architect's analysis, item {item.get('n')}** "
        f"({item.get('verdict')}): {item.get('title') or ''}",
        f"**Shape:** {item.get('shape')}" if item.get("shape") else "",
        str(item.get("analysis") or ""),
        f"**Where:** `{item['evidence']}`" if item.get("evidence") else "",
    ]
    for option in item.get("options") or []:
        if isinstance(option, dict):
            lines.append(f"**Option {option.get('key')}:** "
                         f"{option.get('shape')}. "
                         f"{option.get('tradeoffs') or ''}")
    if item.get("recommendation"):
        lines.append(f"**Recommendation:** {item['recommendation']}")
    if item.get("question"):
        lines.append(f"**Open question:** {item['question']}")
    if build.get("plan"):
        lines.append(f"**Build plan:** {build['plan']}")
    link = _permalink(ts)
    if link:
        lines += ["", f"Slack: {link}"]
    return "\n".join(line for line in lines if line)


def _comment_text(card: dict, items: dict, ts: str) -> str:
    parts = [f"**Feature desk analysis:** {card.get('summary') or ''}"]
    for n, item in sorted(items.items(), key=lambda kv: int(kv[0])):
        parts.append(_item_comment(item, "").strip())
    link = _permalink(ts)
    if link:
        parts.append(f"Slack: {link}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Reading the post
# ---------------------------------------------------------------------------

def _read_post(ts: str) -> dict:
    client = _slack()
    replies = client.conversations_replies(channel=CHANNEL, ts=ts,
                                           limit=50).get("messages") or []
    if not replies:
        return {}
    parent = replies[0]
    # A thread the desk opened itself for a Priority Queue task: the
    # requester is whoever wrote the task, not the bot.
    requester_id = "" if parent.get("bot_id") else str(parent.get("user")
                                                       or "")
    thread = []
    for msg in replies[1:]:
        if msg.get("bot_id"):
            who = "agent"
        elif msg.get("user") == SAM_SLACK:
            who = "Sam"
        else:
            who = _name(msg.get("user"))
        thread.append(f"{who}: {msg.get('text') or ''}")
    text = str(parent.get("text") or "")
    match = _TASK_LINK.search(text)
    return {"text": text, "requester_id": requester_id,
            "requester": _name(requester_id) if requester_id
            else "the task's author (from the Priority Queue)",
            "thread": "\n\n".join(thread),
            "task_id": match.group(1) if match else ""}


# ---------------------------------------------------------------------------
# The Priority Queue: handoffs that never had a Slack post
# ---------------------------------------------------------------------------

def _task_post(task_id: str) -> dict:
    """A ClickUp task standing in for a Slack post. The spec itself goes to
    the architect as `linked_task`; the request is its name and link."""
    task = _clickup("GET", f"/task/{task_id}")
    creator = (task.get("creator") or {}).get("username") or "unknown"
    return {"text": f"{task.get('name')}\n"
                    f"https://app.clickup.com/t/{task_id}",
            "requester_id": "", "requester": creator, "thread": "",
            "task_id": task_id}


def analyse_task(task_id: str) -> dict:
    """Dry run on one Priority Queue task. Nothing is posted or written."""
    return analyse(f"task-{task_id}", dry_run=True,
                   post=_task_post(task_id))


def intake_task(task_id: str) -> dict:
    """Open a thread in #feature-requests for a Priority Queue task and
    analyse it there, so Sam answers it like any other request. Once per
    task: a task the desk already has a thread for is skipped."""
    if not store.ensure_table(LEDGER):
        return {"status": "skipped", "reason": "no database"}
    seen = store.query(f"SELECT ts FROM {LEDGER} WHERE task_id = :t",
                       {"t": task_id})
    if seen:
        return {"status": "skipped", "reason": "already has a thread",
                "ts": seen[0]["ts"]}
    post = _task_post(task_id)
    name = post["text"].split("\n", 1)[0]
    ts = _post(f"*Backend handoff from the Priority Queue* (by "
               f"{post['requester']}): <https://app.clickup.com/t/"
               f"{task_id}|{name}>\nAnalysing it here.")
    if not ts:
        return {"status": "error", "reason": "could not post"}
    _save(ts, task_id=task_id, status=ANALYSING)
    return analyse(ts)


def _name(user_id) -> str:
    if not user_id:
        return "someone"
    try:
        profile = _slack().users_info(user=user_id)["user"]
        return (profile.get("real_name") or profile.get("name")
                or str(user_id))
    except Exception:
        return str(user_id)


def _task_text(task_id: str) -> str:
    """The requester's spec. Ron's backend handoffs carry the endpoint,
    serializer, payloads and open questions here, so it is the main input."""
    if not task_id:
        return "No linked ClickUp task."
    try:
        task = _clickup("GET", f"/task/{task_id}?include_markdown_"
                               "description=true")
    except Exception as exc:
        return f"Linked task {task_id} could not be read ({exc})."
    body = task.get("markdown_description") or task.get("text_content") or ""
    status = (task.get("status") or {}).get("status") or ""
    tags = ", ".join(t.get("name", "") for t in task.get("tags") or [])
    return (f"{task.get('name')} (status {status}; tags {tags or 'none'})"
            f"\n\n{body}")[:14000]


def _existing(text: str) -> str:
    try:
        import clickup_search

        return clickup_search.describe_existing_work(text[:300])
    except Exception as exc:
        return (f"Existing-task search failed ({exc}). Treat existing work "
                "as UNKNOWN, not as none.")


def _previous_text(items: dict) -> str:
    if not items:
        return "(first analysis)"
    rows = []
    for n, item in sorted(items.items(), key=lambda kv: int(kv[0])):
        keys = [o.get("key") for o in item.get("options") or []
                if isinstance(o, dict)]
        rows.append(f"{n}. {item.get('title')}: verdict "
                    f"{item.get('verdict')}, state {item.get('state')}"
                    + (" (FINAL, do not change)" if item.get("state")
                       in FINAL else "")
                    + (f", options {', '.join(keys)}" if keys else ""))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _list(targets: list) -> str:
    return ", ".join(n + opt for n, opt in targets)


def _issue_key(ts: str) -> str:
    return "slack-" + ts.replace(".", "-")


def _row(ts: str) -> dict:
    rows = store.query(f"SELECT * FROM {LEDGER} WHERE ts = :t", {"t": ts})
    return dict(rows[0]) if rows else {}


def _items(row: dict) -> dict:
    try:
        value = json.loads(row.get("items") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _save(ts: str, **fields) -> None:
    if not store.ensure_table(LEDGER):
        return
    cols = ["ts"] + list(fields)
    params = {"ts": ts, **fields}
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields)
    store.execute(
        f"INSERT INTO {LEDGER} ({', '.join(cols)}) VALUES "
        f"({', '.join(':' + c for c in cols)}) ON CONFLICT (ts) DO UPDATE "
        f"SET {updates + ', ' if updates else ''}updated_at = NOW()",
        params,
    )


def _dry(ts: str, card: dict, items: dict, actions: list[str]) -> dict:
    print(f"\n=== {ts} | {card.get('summary')}")
    for n, item in sorted(items.items(), key=lambda kv: int(kv[0])):
        print(f"  {n}. [{item.get('verdict')}] {item.get('title')}"
              f" | shape: {item.get('shape')}")
    for action in actions:
        print(f"WOULD: {action}")
    return {"status": "dry_run", "summary": card.get("summary"),
            "items": items, "actions": actions}


def _slack():
    from slack_sdk import WebClient

    return WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


def _post(text: str, thread_ts: str = "", channel: str = "") -> str:
    try:
        kwargs = {"channel": channel or CHANNEL, "text": text,
                  "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return str(_slack().chat_postMessage(**kwargs).get("ts") or "")
    except Exception as exc:
        print(f"[FEATURE DESK] Slack post failed: {exc}")
        return ""


def _permalink(ts: str) -> str:
    if not ts:
        return ""
    try:
        return str(_slack().chat_getPermalink(
            channel=CHANNEL, message_ts=ts).get("permalink") or "")
    except Exception:
        return ""


def _clickup(method: str, path: str, body: dict | None = None) -> dict:
    resp = httpx.request(
        method, f"https://api.clickup.com/api/v2{path}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _comment(task_id: str, text: str) -> None:
    try:
        _clickup("POST", f"/task/{task_id}/comment",
                 {"comment_text": text, "notify_all": False})
    except Exception as exc:
        print(f"[FEATURE DESK] comment on {task_id} failed: {exc}")


def _set_source(task_id: str) -> None:
    try:
        _clickup("POST", f"/task/{task_id}/field/{FIELD_SOURCE}",
                 {"value": SOURCE_SLACK})
    except Exception as exc:
        print(f"[FEATURE DESK] Source not set on {task_id}: {exc}")


def _link(task_id: str, other: str) -> None:
    if not other:
        return
    try:
        _clickup("POST", f"/task/{task_id}/link/{other}")
    except Exception as exc:
        print(f"[FEATURE DESK] could not link {task_id} to {other}: {exc}")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def backfill(dry_run: bool, limit: int = 10) -> list[dict]:
    """Recent top-level human requests in the channel not yet handled."""
    history = _slack().conversations_history(channel=CHANNEL, limit=100)
    posts = [m for m in history.get("messages") or []
             if not m.get("bot_id") and not m.get("subtype")
             and len((m.get("text") or "").strip()) >= MIN_REQUEST_CHARS]
    out = []
    for msg in reversed(posts[:limit]):
        ts = msg["ts"]
        if not dry_run:
            if _row(ts):
                continue
            _post("Analysing this. I'll check it against the backend and "
                  "reply here with what can be built and what needs Sam.",
                  thread_ts=ts)
        out.append(analyse(ts, dry_run=dry_run))
    return out


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--ts", default="", help="analyse one post, dry run")
    parser.add_argument("--task", nargs="*", default=[],
                        help="dry run on Priority Queue task ids")
    parser.add_argument("--intake", nargs="*", default=[],
                        help="open a thread for each task id and analyse it")
    args = parser.parse_args()
    for task_id in args.task:
        analyse_task(task_id)
    for task_id in args.intake:
        print(task_id, intake_task(task_id))
    if args.ts:
        analyse(args.ts, dry_run=True)
    elif args.backfill:
        results = backfill(args.dry_run, args.limit)
        print(f"\n{len(results)} request(s).")
