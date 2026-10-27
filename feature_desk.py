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
    top-level message (web)  ->  Sam is asked in the thread: analyse it, or
                                 place it as is
                             ->  enqueue engineering.feature_request -> worker
                                 records the post, no model call
    Sam: `analyse`           ->  "Analysing this." and the architect runs:
      1. the post, its thread, the ClickUp task it links, existing tasks
      2. engineering.feature_architect reads the code (production and the
         dev branches) and returns one verdict per request in the post
      3. agent_build    -> engineering.fix_author writes the diff, patch
                           safety, PR into the dev branch, Agent Fixes task
                           (a build that falls through is logged for Sam)
         decision       -> options + a recommendation, Sam is asked
         log            -> logged on the linked task (it is already in
                           Sam's queue); with no task, Sam says `log N`
         already_exists, frontend_only -> the finding, nothing built
         needs_info     -> the requester is asked one question

Ask first (Sam, 2026-10-25): many requests are not wanted yet, or not even
worth an analysis, so nothing is read until Sam says so. Before an analysis
the request is one item, the post as written: `log`, `build`, `route
onlyg|sanjay`, `leave`, `no` and `close` place it with no model call, and
`analyse` (or `go`, or "analyse it, focus on X", which is direction) runs
the architect. Replies from anyone else wait for Sam and cost nothing.

Replies in the thread, from Sam: `go N` / `go NB` (an agent builds item N,
or option B of it), `build N` (Sam Build Now: he builds it himself), `log N`
(Raw Intake, for later), `route N onlyg|sanjay`, `leave N` (stays in his
Priority Queue), `no N`, `close`. He can also say it in his own words
("move Ron's task into Sam Build Now"); the requester's existing task is
moved, never copied, and build now adds no analysis (Sam, 2026-10-21).

Several at once (Sam, 2026-10-27): one command per line ("Go 1A" / "Go
3A" / "log 5"), and `go all` (or "build all recommended") for every open
item as recommended. What builds together is one branch and ONE pull
request per repo, and a later `go` on the same thread joins that pull
request while it is open. An item already there is said and skipped. An
item Sam said go to that turns out more than an agent can build comes back
to him as a question and stays open, never quietly logged. Every build
that stops is also logged in Agent Workflows.
Every other reply, from anyone, is read
against the whole thread (Sam's is also direction on shape), and the agent
chimes in only when it is asked something, a verdict changes, or a reply
contradicts the code. Then it posts a line or two and the items that
changed, never the whole card again. Ron and Sam talking stays theirs.

ClickUp is the record of where a thread landed (Sam, 2026-10-19), so
nothing is written to the linked task while any item is still open. Once
every item is settled (built, logged, declined, already there, frontend
only) the outcome goes on the task once. Sam's `log N` and an opened pull
request are written straight away: both are a settled outcome already.

SWITCHES
--------
The desk always listens to the channel. FEATURE_DESK_ENABLED was removed
at Sam's request, 2026-10-19: a default-off switch nobody set made the first
real post go unanswered.

    FEATURE_DESK_AUTO_ANALYSE     default OFF. "true" goes back to analysing
                                  every new post without asking Sam.
    FEATURE_DESK_PR_ENABLED       default OFF. "true" lets the agent open
                                  pull requests on its own; off, an
                                  agent_build item waits for Sam's `go N`,
                                  which always builds.
    FEATURE_DESK_PR_DAILY_CAP     agent-initiated PRs per day, default 3.
                                  Sam's `go N` is never held by it.
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
PRIORITY_QUEUE = "901113386257"
SAM_ACCEPTED = "901115573620"
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
_COMMAND = re.compile(r"^(go|g|build|mine|log|leave|route|no)\b\s*(.*)$",
                      re.I)
_TARGET = re.compile(r"(\d+)\s*([a-z])?\b", re.I)
# "go all", "go with your recommendations", "build all recommended": every
# open item, as recommended (Sam, 2026-10-27). Nothing else may be left.
_ALL = re.compile(r"\b(?:all|everything|every item|recommended|"
                  r"recommendations?|as|the|with|your|of them|items)\b", re.I)
_RECOMMENDED = re.compile(r"\brecommend", re.I)
_ENGINEER = re.compile(r"\b(?:to\s+)?(onlyg|onlygod|sanjay|backend|frontend)"
                       r"\b", re.I)
_TASK_ID = re.compile(r"\b(86[a-z0-9]{7})\b")
# Sam placing a request in his own words ("Move Ron's task into Sam Build
# Now"). Build now is read first: "log this, I want to personally handle
# it" is his build list, not the backlog (Sam, 2026-10-20).
_SAYS = (
    ("build", re.compile(
        r"\bbuild[ -]?now\b|\bsam accepted\b|\bpersonally handle\b"
        r"|\bi(?:'ll| will) (?:handle|build|take|do) (?:it|this)\b"
        r"|\bi(?:'m| am) (?:building|handling|taking) (?:it|this)\b", re.I)),
    ("route", re.compile(
        r"\b(?:route|send|assign|give|hand)\b.*\b(?:onlyg|onlygod|sanjay)\b"
        r"|\broute\b.*\b(?:backend|frontend)\b", re.I)),
    ("leave", re.compile(
        r"\b(?:leave|keep|park)\b.*\bpriority queue\b"
        r"|\bleave (?:it|this|them)(?: (?:there|for now|as is))?\W*$", re.I)),
    ("log", re.compile(
        r"\braw intake\b|\bbacklog\b|\bfor later\b|\blog the request\b",
        re.I)),
)
# Sam asking for the architect on a request that waits for him. Whatever
# follows the verb is direction ("analyse it, focus on the step type").
_ANALYSE = re.compile(r"^\W*(?:please\s+)?(?:analy[sz]e|look into)"
                      r"(?:\s+(?:it|this|that))?\b\W*(.*)$", re.I | re.S)
# A thank-you is not direction, and must not cost a re-analysis.
_CHAT = re.compile(r"^(thanks?( you)?|ty|ok(ay)?|great|nice|cool|perfect|"
                   r"got it|sounds good|:[a-z0-9_+-]+:)\W*$", re.I)

# Item states. FINAL items are never re-decided by a later analysis.
BUILDING, PR_OPEN, WAITING_SAM, NEEDS_INFO = (
    "building", "pr_open", "waiting_sam", "needs_info")
LOGGED, DECLINED, EXISTS, FRONTEND, HELD = (
    "logged", "declined", "exists", "frontend", "held")
# Where Sam placed it (2026-10-21): his Build Now list, left in his
# Priority Queue, or with an engineer. LOGGED doubles as Raw Intake.
BUILD_NOW, LEFT, ROUTED = "build_now", "left", "routed"
FINAL = (PR_OPEN, LOGGED, DECLINED, EXISTS, FRONTEND, BUILD_NOW, LEFT,
         ROUTED)
# Sam can still move these: an architect's `log` only left the task where
# it was, and a placed item can be placed again.
PLACEABLE = ("", WAITING_SAM, HELD, NEEDS_INFO, LOGGED, LEFT, BUILD_NOW,
             ROUTED)
# Sam's `go` has nothing to build on these.
GO_SKIPS = (PR_OPEN, EXISTS, FRONTEND)
PLACES = {"build": "Sam Build Now", "log": "Raw Intake",
          "leave": "Sam's Priority Queue"}
ENGINEER_OPEN = ("queued", "in progress", "escalated")

# Post states. AWAITING: recorded, nothing analysed until Sam says.
ANALYSING, OPEN, CLOSED, ERROR = "analysing", "open", "closed", "error"
AWAITING = "awaiting_sam"
# Every item landed and the outcome is on ClickUp: the post gets a check
# mark so the channel shows what is handled at a glance (Sam, 2026-10-19).
SETTLED = "settled"
DONE_EMOJI = "white_check_mark"

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


def auto_analyse() -> bool:
    return _on("FEATURE_DESK_AUTO_ANALYSE")


def pr_enabled() -> bool:
    return _on("FEATURE_DESK_PR_ENABLED")


BEYOND_AGENT = ("You said go, but this is more than an agent can build on "
                "its own (see above). Tell me how to narrow it and I'll add "
                "it to the same pull request, or `build {n}` (you build "
                "it), `log {n}` (Raw Intake for later).")
ANALYSING_TEXT = ("Analysing this. I'll check it against the backend and "
                  "reply here with what can be built and what needs Sam.")
ASK_SAM_TEXT = (f"<@{SAM_SLACK}> analyse this, or place it as is? "
                "`analyse` (the architect checks it against the backend), "
                "`log` (Raw Intake), `build` (Sam Build Now), `route onlyg` "
                "or `sanjay`, `leave` (stays in your Priority Queue), `no`.")


def daily_cap() -> int:
    return int(os.environ.get("FEATURE_DESK_PR_DAILY_CAP", "3"))


# ---------------------------------------------------------------------------
# Web side: the Slack event
# ---------------------------------------------------------------------------

def handle_message(event: dict) -> None:
    """One human message in #feature-requests. Cheap, never raises."""
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
            _post(ANALYSING_TEXT if auto_analyse() else ASK_SAM_TEXT,
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
    if not ts:
        return {"status": "skipped", "reason": "no message"}
    if not store.ensure_table(LEDGER):
        return {"status": "skipped", "reason": "no database"}
    try:
        reply = message.get("reply")
        if reply:
            return _on_reply(ts, reply)
        row = _row(ts)
        if row and row.get("status") != ERROR:
            return {"status": "skipped", "reason": "already handled"}
        if not auto_analyse():
            return _hold(ts)
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
    if row.get("status") == CLOSED:
        return {"status": "skipped", "reason": "thread closed"}

    sam = reply.get("user") == SAM_SLACK
    if sam:
        if text.lower().rstrip(".! ") == "close":
            _save(ts, status=CLOSED)
            _react(ts)
            _post("Closed. Nothing more will be built or filed from this "
                  "thread.", thread_ts=ts)
            return {"status": "ok", "action": "closed"}
        placement = parse_placement(text, items)
        commands = parse_commands(text, items) or (
            [placement] if placement else None)
        if row.get("status") == AWAITING:
            asked = _ANALYSE.match(text)
            if asked or any(c["verb"] == "go" for c in commands or []):
                return _analyse_now(ts, asked.group(1).strip() if asked
                                    else "")
        if commands:
            result = {}
            for i, command in enumerate(commands):
                if i:
                    # The command before may have saved new item states.
                    row = _row(ts) or row
                    items = _items(row) or items
                result = _command(ts, row, items, command)
            return result
    if row.get("status") == AWAITING:
        # Nothing is read until Sam asks for it: that is the saving.
        return {"status": "skipped", "reason": "waiting for Sam"}
    if _CHAT.match(text):
        return {"status": "skipped", "reason": "acknowledgement"}
    # Anything else is read against the whole thread, and the architect
    # decides whether it has anything to add. Sam's words are also
    # direction on shape.
    who = "Sam" if sam else _name(reply.get("user"))
    return analyse(ts, direction=text if sam else "",
                   reply=f"{who}: {text}")


def _hold(ts: str) -> dict:
    """A new post, recorded with no model call (Sam, 2026-10-25). It is one
    item, the post as written, so Sam can place it without an analysis."""
    post = _read_post(ts)
    if not post:
        return {"status": "skipped", "reason": "message unreadable"}
    _save(ts, status=AWAITING, requester=post["requester"],
          task_id=post["task_id"], items=json.dumps({"1": _plain_item(post)}))
    return {"status": "ok", "action": "waiting for Sam"}


def _plain_item(post: dict) -> dict:
    """The request as written, standing in for the architect's items.
    Recorded already: there is no analysis to write on the task."""
    text = re.sub(r"<[^>]*>", " ", str(post.get("text") or ""))
    first = next((line.strip(" *_#-") for line in text.splitlines()
                  if line.strip(" *_#-")), "")
    return {"n": "1", "title": first[:120] or "Feature request",
            "state": "", "recorded": True}


def _analyse_now(ts: str, direction: str) -> dict:
    """Sam asked for the architect. The stand-in item goes, so the
    architect's own items are the only ones on the thread."""
    _save(ts, status=ANALYSING, items=json.dumps({}))
    _post(ANALYSING_TEXT, thread_ts=ts)
    return analyse(ts, direction=direction)


def parse_command(text: str, items: dict) -> dict | None:
    """`go 2`, `go 2B`, `go 1 3`, `build 1`, `log 3`, `leave 1`,
    `route 1 onlyg`, `no 4`, or a bare verb meaning every open item. None
    when the message is not exactly a command, so "go with B but keep it on
    the role" reads as direction, not as `go`."""
    match = _COMMAND.match(text.strip().strip("`*_").rstrip(".!"))
    if not match:
        return None
    verb, rest = match.group(1).lower(), match.group(2).strip()
    verb = {"mine": "build", "g": "go"}.get(verb, verb)
    engineer = ""
    if verb == "route":
        found = _ENGINEER.search(rest)
        engineer = found.group(1).lower() if found else ""
        rest = _ENGINEER.sub(" ", rest).strip()
    if rest and _ALL.search(rest) and not _ALL.sub("", rest).strip(" ,.!"):
        # "build all recommended" is the agent building them, not Sam's
        # Build Now list: the recommendation is what gets built.
        if verb == "build" and _RECOMMENDED.search(rest):
            verb = "go"
        rest = ""
    targets = []
    if rest:
        cleaned = re.sub(r"\b(and|item|items)\b|[,&]", " ", rest, flags=re.I)
        if _TARGET.sub("", cleaned).strip():
            return None
        targets = [(n, (opt or "").upper())
                   for n, opt in _TARGET.findall(cleaned)]
    else:
        targets = _open_targets(verb, items)
    if not targets:
        return None
    command = {"verb": verb, "targets": targets}
    if verb == "route":
        command["engineer"] = engineer
    return command


def parse_commands(text: str, items: dict) -> list[dict] | None:
    """One command per line ("Go 1A" / "Go 3A" / "log 5"), or several on
    one line split by `;`. Every line must be a command (a thank-you line is
    skipped), else the whole message is direction. The same verb merges into
    one command, and `go` runs last so its builds share one pull request.

    Sam, 2026-10-27: six `go` lines in one message were read as free text,
    and the desk answered "go 3 opens the pull request" to a go 3."""
    lines = [line for line in re.split(r"[\n;]+", text)
             if line.strip(" `*_.!")]
    if len(lines) < 2:
        command = parse_command(text, items)
        return [command] if command else None
    merged: dict[tuple, dict] = {}
    for line in lines:
        if _CHAT.match(line.strip()):
            continue
        command = parse_command(line, items)
        if not command:
            return None
        key = (command["verb"], command.get("engineer", ""))
        if key not in merged:
            merged[key] = command
            continue
        seen = merged[key]["targets"]
        seen += [t for t in command["targets"] if t not in seen]
    if not merged:
        return None
    return sorted(merged.values(), key=lambda c: c["verb"] == "go")


def parse_placement(text: str, items: dict) -> dict | None:
    """Sam saying where a request goes in his own words: "Move Ron's task
    into Sam Build Now", "send it to OnlyG", "leave it in my priority
    queue", "backlog it". A question is not an instruction. Item numbers
    in the message narrow it; otherwise it is every open item. A ClickUp
    task named in the message is the one to move (the work already has a
    task, so nothing new is created)."""
    clean = text.strip()
    if not clean or clean.rstrip("`*_ ").endswith("?"):
        return None
    for verb, said in _SAYS:
        if not said.search(clean):
            continue
        stripped = _TASK_LINK.sub(" ", clean)
        numbers = [n for n in re.findall(r"\b(\d)\b", stripped) if n in items]
        targets = ([(n, "") for n in numbers] if numbers
                   else _open_targets(verb, items))
        if not targets:
            return None
        command = {"verb": verb, "targets": targets}
        task = _TASK_LINK.search(clean) or _TASK_ID.search(stripped)
        if task:
            command["task"] = task.group(1)
        if verb == "route":
            found = _ENGINEER.search(clean)
            command["engineer"] = found.group(1).lower() if found else ""
        return command
    return None


def _open_targets(verb: str, items: dict) -> list[tuple[str, str]]:
    if verb in ("build", "log", "leave", "route"):
        return [(n, "") for n, item in items.items()
                if item.get("state") in PLACEABLE]
    if verb == "go":
        # Logged by the architect, not placed by Sam: `go all` means it too
        # (2026-10-27, items 5 and 6 of the People table thread).
        return [(n, "") for n, item in items.items()
                if item.get("state") not in FINAL
                or (item.get("state") == LOGGED and not item.get("placed")
                    and not item.get("clickup"))]
    return [(n, "") for n, item in items.items()
            if item.get("state") not in FINAL]


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
        _record(ts, row, items)
        return {"status": "ok", "action": "declined"}

    if verb in ("build", "log", "leave", "route"):
        return _place(ts, row, items, command)

    # go: an item already planned for an agent builds now; anything else
    # is re-decided with Sam's choice as direction, because the build plan
    # for the option he picked has not been written yet.
    # LOGGED counts while nothing is recorded yet: a build that fell through
    # before 2026-10-19 was logged, and Sam's `go` is how it is retried.
    # Already there, frontend only or already in a PR: nothing to build, so
    # it is said and left (2026-10-27: `go 1A` on an item that was already
    # there went back to the architect as direction).
    done = [n for n, _ in command["targets"]
            if items[n].get("state") in GO_SKIPS]
    if done:
        _post("Nothing to build for "
              + ", ".join(f"{n} ({_landed(items[n])})" for n in done) + ".",
              thread_ts=ts)
        command = {**command, "targets": [t for t in command["targets"]
                                          if t[0] not in done]}
        if not command["targets"]:
            return {"status": "ok", "action": "nothing to build"}
    ready = [n for n, opt in command["targets"]
             if not opt and items[n].get("verdict") == "agent_build"
             and not items[n].get("pr_url")
             and (items[n].get("state") in (HELD, WAITING_SAM)
                  or (items[n].get("state") == LOGGED
                      and not items[n].get("recorded")))]
    rest = [(n, opt) for n, opt in command["targets"] if n not in ready]
    for n, _ in rest:
        if items[n].get("state") in FINAL:
            # Logged, left or placed, and Sam now says go: it is open again,
            # or the re-analysis below could never touch it (_merge).
            items[n].update(state="", recorded=False)
    if rest:
        _save(ts, items=json.dumps(items))
    results = _build_all(ts, row, items, ready, sam_approved=True)
    _record(ts, row, items)
    if rest:
        direction = "; ".join(
            f"Sam: build item {n}" + (f" using option {opt}" if opt else
                                      " using your recommendation")
            for n, opt in rest)
        return analyse(ts, direction=direction,
                       approved=[n for n, _ in rest])
    return {"status": "ok", "action": "built", "results": results}


def analyse(ts: str, direction: str = "", approved: list | None = None,
            dry_run: bool = False, post: dict | None = None,
            reply: str = "") -> dict:
    """Run the architect on the post and act on each item's verdict.
    `post` stands in for the Slack message when a ClickUp task is
    analysed without one (a dry run of the Priority Queue). `reply` is the
    thread message that woke it: only the items whose verdict it changed
    are acted on and reposted, the rest keep exactly what they had."""
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
        "new_reply": reply or "(none: first analysis)",
    }
    result = run_agent(AGENT, context=context,
                       subject_type="slack_feature_request", subject_id=ts)
    if not result.ok and reply:
        # Nobody asked for this read, so a failed one stays out of the
        # thread rather than turning into a question for Sam.
        print(f"[FEATURE DESK] reply read failed on {ts}: {result.why()}")
        return {"status": "skipped", "reason": "reply read failed"}
    if not result.ok:
        card = {"summary": "The analysis did not finish", "items": [{
            "n": 1, "title": "Request", "verdict": "decision",
            "analysis": f"The architect returned nothing usable "
                        f"({result.why()}).",
            "question": "Can you take a look at this one?"}]}
    else:
        card = result.data or {}

    items = _merge(previous, card.get("items") or [], set(approved or []))
    changed = None
    if reply:
        changed = [n for n, item in items.items()
                   if item.get("state") not in FINAL
                   and (n not in previous or previous[n].get("verdict")
                        != item.get("verdict"))]
        for n in items:
            if n in previous and n not in changed:
                items[n] = previous[n]
    if not dry_run:
        _save(ts, requester=post["requester"], task_id=task_id,
              card=json.dumps(card), items=json.dumps(items),
              rounds=rounds,
              # Chat after the check mark must not earn a second one.
              status=SETTLED if row.get("status") == SETTLED else OPEN)
    row = {**row, "ts": ts, "task_id": task_id,
           "requester": post["requester"],
           "requester_id": post["requester_id"], "rounds": rounds}
    return act(ts, row, card, items, dry_run=dry_run, only=changed)


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
        dry_run: bool = False, only: list | None = None) -> dict:
    """One thread reply with every item, then the builds, each reporting
    back in the thread when its pull request opens or falls through.
    `only` is a reply round: just those items, led by the architect's own
    line, and nothing posted at all when it has nothing to add."""
    if only is None and card.get("not_a_request") and not any(
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
        if item.get("state") in FINAL or (only is not None and n not in only):
            continue
        verdict = str(item.get("verdict") or "decision")
        if verdict == "needs_info" and over_rounds:
            verdict = item["verdict"] = "decision"
            item["question"] = (f"I've asked for more information "
                                f"{MAX_ROUNDS} times. Can you take it?")
        open_check = _unchecked(item) if verdict == "agent_build" else ""
        if open_check:
            # Never offered as ready while the architect says it has not
            # looked: Sam is asked, and his answer re-runs the analysis.
            verdict = item["verdict"] = "decision"
            item["question"] = (f"{open_check}. Should I check that before "
                                "building, or build it as planned?")
        if verdict == "agent_build":
            ok, why = _can_build(item)
            if ok and (pr_enabled() or item.get("sam_approved")):
                item["state"] = BUILDING
                to_build.append(n)
            elif ok:
                # PRs wait for Sam: say so once, in this reply, rather than
                # "building now" followed by a second message that it is not.
                item["state"] = HELD
                asks_sam = True
            else:
                item["verdict"], item["state"] = "log", ""
                item["analysis"] = (f"{item.get('analysis') or ''} "
                                    f"Not built by an agent: {why}.").strip()
                verdict = "log"
        if verdict == "log" and item.get("sam_approved"):
            # Sam said go. Work the agent can't take on is a question back
            # to him and stays open, never quietly logged (2026-10-27: his
            # `go 5A` and `go 6A` came back "Logged for Sam").
            verdict = item["verdict"] = "decision"
            item["question"] = BEYOND_AGENT.format(n=n)
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

    said = str(card.get("reply") or "").strip()
    chimed = bool(card.get("chime_in") and said)
    if only is not None and not lines and not chimed:
        if not dry_run:
            _save(ts, items=json.dumps(items))
            _record(ts, row, items)
        else:
            _dry(ts, card, items, ["nothing (read the reply, stayed quiet)"])
        return {"status": "ok", "action": "read, stayed quiet"}
    if not lines and only is None:
        if not dry_run:
            _post("Nothing is still open on this request.", thread_ts=ts)
        return {"status": "skipped", "reason": "nothing open"}

    header = said if only is not None else \
        f"*{card.get('summary') or 'Request'}*"
    footer = []
    if asks_sam:
        footer.append(f"<@{SAM_SLACK}> reply `go N` (agent builds it, "
                      "`go NB` for option B), `go all` (every item as "
                      "recommended, one pull request), `build N` (Sam Build "
                      "Now), `log N` (Raw Intake), `route N onlyg` or "
                      "`sanjay`, `leave N` (stays in your Priority Queue), "
                      "`no N`, or tell me how you'd shape it. One per line "
                      "is fine.")
    if any(i.get("state") == NEEDS_INFO for n, i in items.items()
           if only is None or n in only):
        who = row.get("requester_id") or ""
        footer.append(f"{f'<@{who}> ' if who else ''}an answer here and "
                      "I'll look again.")
    text = "\n\n".join(part for part in [header, *lines, *footer] if part)

    if dry_run:
        plans = [f"build {n}: {items[n].get('build')}" for n in to_build]
        return _dry(ts, card, items, [f"thread:\n{text}", *plans])

    _post(text, thread_ts=ts)
    _save(ts, items=json.dumps(items))

    results = _build_all(ts, row, items, to_build)
    _record(ts, row, items, card.get("summary") or "")
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
    elif item.get("state") == HELD:
        lines.append(f"Ready to build. `go {item['n']}` opens the pull "
                     "request.")
    if verdict == "log" and item.get("state") == LOGGED:
        lines.append("Stays on the linked task in your Priority Queue. "
                     + _place_hint(item["n"]))
    elif verdict == "log":
        lines.append(_place_hint(item["n"]))
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
    if not anchors or len(anchors) > MAX_ANCHORS:
        return False, (f"needs one to {MAX_ANCHORS} exact file:line "
                       "anchors")
    if not all(re.match(r"^[\w./-]+\.py:\d+$", a) for a in anchors):
        return False, f"anchors are not exact path:line ({anchors})"
    if len({a.rsplit(":", 1)[0] for a in anchors}) > MAX_BUILD_FILES:
        return False, f"edits more than {MAX_BUILD_FILES} files"
    return True, ""


# The envelope is three files, not three edits. A helper and its call site
# can sit 600 lines apart in one file, and the builder only sees a window
# around each anchor (2026-10-24: three anchors spread over three files
# left the V4 _get_shifts call site unseen, and every `go 1` refused).
MAX_BUILD_FILES = 3
MAX_ANCHORS = 6


# The architect's own words for something it has not looked at yet. An
# agent_build carrying one is not ready: 2026-10-21, "Not yet checked:
# whether is_offline_volunteer is cleared on claim" reached the builder,
# which stopped on it.
_UNCHECKED = re.compile(r"\bnot (?:yet )?(?:checked|verified|confirmed)\b",
                        re.I)

# A fix author refusing because it was not shown enough, in its own words.
# The structured `needs_context` field is the real signal; this catches a
# reason written before that field existed or without it.
_MISSING_SOURCE = re.compile(
    r"\b(?:cannot|can't|could not|did not) see\b|\bnot shown\b"
    r"|\bnever shown\b|\bnot (?:visible|included) in the source\b"
    r"|\bto proceed,? i need\b|\bneeds? more (?:source|context)\b", re.I)


def _unchecked(item: dict) -> str:
    """The open check in an agent_build's analysis or plan, else ''."""
    build = item.get("build") if isinstance(item.get("build"), dict) else {}
    for text in (item.get("analysis"), build.get("plan")):
        match = _UNCHECKED.search(str(text or ""))
        if match:
            return str(text)[match.start():].split(". ")[0].strip(" .")
    return ""


def _needs_context(proposal: dict) -> bool:
    if proposal.get("needs_context"):
        return True
    return bool(_MISSING_SOURCE.search(str(proposal.get("reason") or "")))


def _build_all(ts: str, row: dict, items: dict, ns: list,
               sam_approved: bool = False) -> list:
    """Items built together share one branch and one pull request per repo,
    and join the request's pull request while it is still open (Sam,
    2026-10-27: a request with several items is one change, reviewed and
    merged once). An item that stops is said in the thread and the rest
    still go."""
    batches: dict[str, dict] = {}
    results = []
    for n in ns:
        alias = str((items[n].get("build") or {}).get("repo") or "")
        batch = batches.setdefault(alias, _batch(items, alias))
        results.append(_build(ts, row, items, n, sam_approved=sam_approved,
                              batch=batch))
    finished = {}
    for batch in batches.values():
        finished.update({r["n"]: r for r in _finish(ts, row, items, batch)})
    return [finished.get(r.get("n"), r) if isinstance(r, dict) else r
            for r in results]


def _batch(items: dict, alias: str) -> dict:
    """Where this request's builds in one repo go: the branch of its pull
    request that is still open, else a new branch cut on the first
    commit."""
    for item in items.values():
        if (item.get("state") == PR_OPEN and item.get("pr_branch")
                and item.get("pr_url") and not item.get("merged")
                and (item.get("build") or {}).get("repo") == alias):
            return {"branch": item["pr_branch"], "pr_url": item["pr_url"],
                    "fix_task": item.get("fix_task") or "", "base": "",
                    "repo": None, "written": []}
    return {"branch": "", "pr_url": "", "fix_task": "", "base": "",
            "repo": None, "written": []}


def _build(ts: str, row: dict, items: dict, n: str,
           sam_approved: bool = False, batch: dict | None = None) -> dict:
    """fix_author writes the diff, patch safety checks it, and it is
    committed to the batch's branch. Anything that stops it is said in the
    thread, and the item goes to Sam rather than disappearing. With no
    batch it is a batch of one and its pull request opens here."""
    item = items[n]
    sam_approved = sam_approved or bool(item.get("sam_approved"))
    if sam_approved:
        item["sam_approved"] = True
    ok, why = _can_build(item)
    if not ok:
        # The plan itself is unbuildable; no word of Sam's changes that.
        return _fell_through(ts, row, items, n, why)
    # FEATURE_DESK_PR_ENABLED and the daily cap only stop the agent opening
    # pull requests on its own. Sam's `go N` always builds (Sam, 2026-10-19:
    # his `go 1` was refused with "say go 1", which he had just said).
    if not sam_approved:
        if not pr_enabled():
            why = "the agent waits for your word before opening a PR"
        elif _opened_today() >= daily_cap():
            why = f"the daily cap of {daily_cap()} agent PRs is reached"
        if why:
            item["state"] = HELD
            _save(ts, items=json.dumps(items))
            _post(f"*{n}* is ready to build ({why}). <@{SAM_SLACK}> say "
                  f"`go {n}` to open the pull request, or `log {n}`.",
                  thread_ts=ts)
            return {"n": n, "built": False, "reason": why}

    own = batch is None
    if own:
        batch = _batch(items, str((item.get("build") or {}).get("repo")))
    # After the first item the builder reads the branch, so a second item
    # in the same file is written on top of the first, not against it.
    proposal, repo = _propose(ts, item, ref=batch["branch"])
    if not proposal.get("diff"):
        reason = proposal.get("reason") or "no diff"
        if _needs_context(proposal):
            # The plan may be fine; the builder was not shown enough source
            # to write it. That is fixable, so the item waits for `go N`
            # instead of being logged as if the plan were wrong (2026-10-21:
            # Sam's `go 1` on profile status was logged over missing imports).
            item["state"] = HELD
            item["build_stopped"] = reason
            # Every path:line the builder asked for comes with the next
            # `go N`, or the retry is shown the same source and refuses the
            # same way (2026-10-24: it named v4/api.py:1328 and the second
            # `go 1` still stopped short of it).
            import sentry_code
            asked = item.get("source_asked") or []
            for path, line in sentry_code.references(reason):
                if f"{path}:{line}" not in asked:
                    asked.append(f"{path}:{line}")
            item["source_asked"] = asked
            _save(ts, items=json.dumps(items))
            _post(f"*{n}* needs more source than the builder was given: "
                  f"{reason.rstrip('. ')}. <@{SAM_SLACK}> say `go {n}` to "
                  f"try again, or `log {n}`."
                  + _failure_line(ts, row, item, reason), thread_ts=ts)
            return {"n": n, "built": False, "reason": reason}
        return _fell_through(ts, row, items, n,
                             f"the builder declined: {reason}")
    batch["repo"] = repo
    written = _write(ts, item, proposal, repo, batch)
    if not written.get("ok"):
        # The diff was written and checked; only the host refused (a token,
        # a permission, a network error). That is fixable, so the item
        # waits for `go N` again instead of being logged as if the plan
        # were wrong (2026-10-19: a 403 on branch creation logged item 1).
        why = str(written.get("reason") or "unknown")
        item["state"] = HELD
        _save(ts, items=json.dumps(items))
        _post(f"*{n}* is written and passed the safety check, but the pull "
              f"request couldn't open: {why}. <@{SAM_SLACK}> once that's "
              f"fixed, say `go {n}` to try again."
              + _failure_line(ts, row, item, why), thread_ts=ts)
        return {"n": n, "built": False, "reason": why}
    item.update(sam_approved=sam_approved,
                risks=str(proposal.get("risks") or ""))
    batch["written"].append(n)
    _save(ts, items=json.dumps(items))
    if own:
        return (_finish(ts, row, items, batch) or [{"n": n}])[0]
    return {"n": n, "built": True}


def _fell_through(ts: str, row: dict, items: dict, n: str,
                  why: str) -> dict:
    """A build that stopped for good. Logged for Sam, unless Sam said go:
    then it stays open on the thread as a question (Sam, 2026-10-27)."""
    item = items[n]
    item["analysis"] = f"{item.get('analysis') or ''} Build stopped: " \
                       f"{why}.".strip()
    if item.get("sam_approved"):
        item.update(state=WAITING_SAM, verdict="decision",
                    question=BEYOND_AGENT.format(n=n))
        line = f"<@{SAM_SLACK}> {item['question']}"
    else:
        line = _log(ts, row, items, n)
    _save(ts, items=json.dumps(items))
    _post(f"*{n}* didn't build ({why}). {line}"
          + _failure_line(ts, row, item, why), thread_ts=ts)
    return {"n": n, "built": False, "reason": why}


def _failure_line(ts: str, row: dict, item: dict, why: str) -> str:
    """A build that stopped, in Agent Workflows for Sam and Claude to work
    through later (Sam, 2026-10-20: every workflow failure is logged)."""
    import workflow_log

    build = item.get("build") or {}
    url = workflow_log.log_issue(
        area="feature desk",
        title=f"Feature desk build stopped: {item.get('title') or 'item'}"[
            :150],
        summary=f"Item {item.get('n')} of a #feature-requests post did not "
                f"build. Reason: {str(why).rstrip('. ')}.",
        details="\n".join([
            f"- Reason: {why}",
            f"- Plan: {build.get('plan') or '(none)'}",
            f"- Repo: {build.get('repo') or '(none)'}",
            f"- Anchors: {', '.join(build.get('anchors') or []) or '(none)'}",
            f"- Evidence: `{item.get('evidence') or ''}`",
            f"- Requester's task: https://app.clickup.com/t/{row['task_id']}"
            if row.get("task_id") else "- Requester's task: (none)",
        ]),
        channel=CHANNEL, thread_ts=ts,
        next_step="Work out with Claude what the desk or the builder needs, "
                  "fix it, then say go again in the thread.",
    )
    return (f"\nLogged for review in Agent Workflows: <{url}|task>"
            if url else "")


def _propose(ts: str, item: dict, ref: str = ""):
    """The fix author's diff for the plan, against the dev branch it will
    merge into (or `ref`, the request's branch once an item is on it). Each
    edited file comes with its import blocks, and every path:line the
    evidence or plan cites comes as read-only reference, so the builder can
    add an import and copy a precedent it was told to copy (2026-10-21:
    three bare windows made it refuse, rightly)."""
    import sentry_code
    import sentry_fix
    import sentry_projects
    from vomeos.tools import repo_map

    build = item.get("build") or {}
    repo = repo_map().get(str(build.get("repo") or ""))
    if not repo:
        return {"reason": f"unknown repo {build.get('repo')!r}"}, repo
    ref = ref or sentry_projects.fix_target(repo.slug) or repo.branch
    anchors = [a.rsplit(":", 1) for a in build.get("anchors") or []]
    refs = sentry_code.references(
        f"{item.get('evidence') or ''}\n{build.get('plan') or ''}\n"
        f"{item.get('build_stopped') or ''}\n"
        + " ".join(item.get("source_asked") or []))
    gathered = sentry_code.gather_for_build(
        [(path, int(line)) for path, line in anchors], repo.slug, ref,
        host=repo.host, owner=repo.owner, refs=refs)
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


def _write(ts: str, item: dict, proposal: dict, repo, batch: dict) -> dict:
    """Apply the diff and commit it to the batch's branch, cutting the
    branch from the dev branch on the first item. The same apply-and-verify
    step as sentry_fix.open_pull_request. {"ok"} or {"reason"}."""
    import sentry_fix
    import sentry_projects
    from vomeos.integrations import code_write

    base = sentry_projects.fix_target(repo.slug)
    if not base:
        return {"ok": False, "reason": f"no dev branch for {repo.slug}"}
    batch["base"] = base
    contents, error = sentry_fix._apply_to_repo(
        repo.slug, batch["branch"] or base, repo.host, repo.owner,
        proposal["diff"])
    if error:
        return {"ok": False, "reason": f"patch does not apply: {error}"}

    writer = code_write.writer_for(repo.host)
    kwargs = {"owner": repo.owner} if repo.host == "github" and repo.owner \
        else {}
    if not writer.configured(**kwargs):
        return {"ok": False, "reason": "no write token configured"}

    title = str(item.get("title") or "Requested backend change")
    message = f"Feature request: {title}"[:100]
    try:
        if not batch["branch"]:
            branch = code_write.branch_name(
                f"feature-{_issue_key(ts)}-{item['n']}", title[:40])
            created = writer.create_branch(repo.slug, branch, base, **kwargs)
            if not created.ok:
                return {"ok": False, "reason": "could not branch: "
                                               + _host_error(created.error)}
            batch["branch"] = branch
        for path, body in contents.items():
            written = writer.commit_file(repo.slug, batch["branch"], path,
                                         body, message, **kwargs)
            if not written.ok:
                return {"ok": False,
                        "reason": f"could not commit {path}: "
                                  + _host_error(written.error)}
    except code_write.WriteDenied as exc:
        return {"ok": False, "reason": f"refused: {exc}"}
    return {"ok": True}


def _finish(ts: str, row: dict, items: dict, batch: dict) -> list[dict]:
    """One pull request for everything the batch wrote, or the items added
    to the request's open one. One thread message and one Agent Fixes task
    either way."""
    ns = batch["written"]
    if not ns:
        return []
    repo = batch["repo"]
    alias = getattr(repo, "alias", "the repo")
    built = [items[n] for n in ns]
    label = ", ".join(ns)
    added = bool(batch["pr_url"])
    if added:
        url, fix_task = batch["pr_url"], batch["fix_task"]
        if fix_task:
            _comment(fix_task, "Added to the same pull request:\n"
                     + "\n".join(_fix_lines(item) for item in built))
    else:
        pr = _open_pr(ts, row, built, repo, batch)
        if not pr.get("opened"):
            why = str(pr.get("reason") or "unknown")
            for item in built:
                item["state"] = HELD
            _save(ts, items=json.dumps(items))
            _post(f"*{label}* written and passed the safety check, but the "
                  f"pull request couldn't open: {why}. <@{SAM_SLACK}> once "
                  f"that's fixed, say `go {' '.join(ns)}` to try again."
                  + _failure_line(ts, row, built[0], why), thread_ts=ts)
            return [{"n": n, "built": False, "reason": why} for n in ns]
        url = pr["url"]
        fix_task = _file_fix_task(built, repo, url, ts, row)
    task_url = f"https://app.clickup.com/t/{fix_task}" if fix_task else ""
    for item in built:
        item.update(state=PR_OPEN, pr_url=url, fix_task=fix_task,
                    pr_day=_today(), pr_branch=batch["branch"])
    if row.get("task_id"):
        _comment(row["task_id"],
                 f"Item{'s' if len(ns) > 1 else ''} {label} built by an "
                 f"agent: {url}\nAgent Fixes task: {task_url}")
        if not added:
            _link(row["task_id"], fix_task)
        for item in built:
            item["recorded"] = True
    _save(ts, items=json.dumps(items))
    lines = [f"*{label}. Added to the open pull request:* <{url}|into "
             f"{alias} development>." if added else
             f"*{label}. Pull request open:* <{url}|into {alias} "
             f"development>. Sam reviews and merges."]
    for item in built:
        build = item.get("build") or {}
        which = f"{item['n']}. " if len(built) > 1 else ""
        if build.get("contract"):
            lines.append(f"_{which}What the frontend gets:_ "
                         f"{build['contract']}")
        if item.get("risks"):
            lines.append(f"_{which}Unsure about:_ {item['risks']}")
    if task_url and not added:
        lines.append(f"ClickUp: <{task_url}|Agent Fixes task>")
    # The PR line first, then the check mark if this was the last open item.
    _post("\n".join(lines), thread_ts=ts)
    _record(ts, row, items)
    return [{"n": n, "built": True, "pr_url": url} for n in ns]


def _open_pr(ts: str, row: dict, built: list, repo, batch: dict) -> dict:
    """The pull request from the batch's branch into the dev branch, with a
    body that describes requested changes rather than a bug."""
    import sentry_fix
    from vomeos.integrations import code_write

    writer = code_write.writer_for(repo.host)
    kwargs = {"owner": repo.owner} if repo.host == "github" and repo.owner \
        else {}
    titles = [str(i.get("title") or "Requested backend change")
              for i in built]
    title = titles[0] + (f" (+{len(titles) - 1} more)"
                         if len(titles) > 1 else "")
    message = f"Feature request: {title}"[:100]
    try:
        opened = writer.open_pull_request(
            repo.slug, batch["branch"], batch["base"], title=message,
            body=_pr_body(ts, row, built), **kwargs)
    except code_write.WriteDenied as exc:
        return {"opened": False, "reason": f"refused: {exc}"}
    if not opened.ok:
        return {"opened": False,
                "reason": "could not open PR: " + _host_error(opened.error)}
    return {"opened": True, "url": sentry_fix._pr_url(opened.data),
            "branch": batch["branch"]}


def _host_error(error: str) -> str:
    """The host's refusal in words Sam can act on. A missing token scope
    otherwise arrives as raw JSON cut off mid-word in Slack."""
    text = str(error or "")
    if "privilege scopes" in text or "scope" in text.lower() and "403" in text:
        return ("the write token lacks a required scope. "
                "VOMEOS_BITBUCKET_WRITE_TOKEN on vome-os-worker needs "
                "repository write and pull request write")
    return text[:200]


def _pr_body(ts: str, row: dict, built: list) -> str:
    link = _permalink(ts)
    lines = [
        "Built automatically from a #feature-requests post. **Not reviewed "
        "by a person.** The architect judged each item an additive change "
        "that follows an existing pattern; check that it does.",
        "",
        f"**Requested by** {row.get('requester') or 'a teammate'}"
        + (f" · [Slack]({link})" if link else "")
        + (f" · [ClickUp](https://app.clickup.com/t/{row['task_id']})"
           if row.get("task_id") else ""),
    ]
    for item in built:
        build = item.get("build") or {}
        lines += [
            "",
            f"## {item.get('n')}. {item.get('title') or ''}"
            if len(built) > 1 else "### What was asked",
            "" if len(built) > 1 else str(item.get("title") or ""),
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
            str(item.get("risks") or "none identified"),
        ]
    return "\n".join(lines + [
        "",
        "---",
        "No migration, no new model, no auth, permission or billing code: "
        "the patch safety check refuses all of them.",
    ])


def _fix_lines(item: dict) -> str:
    build = item.get("build") or {}
    return "\n".join([
        f"**{item.get('n')}. {item.get('title') or ''}**",
        f"**Shape:** {item.get('shape') or ''}",
        f"**Plan:** {build.get('plan') or ''}",
        f"**Contract:** {build.get('contract') or ''}",
        f"**Precedent:** `{item.get('evidence') or ''}`",
    ])


def _file_fix_task(built: list, repo, pr_url: str, ts: str,
                   row: dict) -> str:
    """The Agent Fixes parent, one per pull request. Its "Fix PR:" line is
    what ticket_fix.promote_merged follows to `on dev` once the PR merges,
    and the release watcher proves it live from there."""
    import ticket_fix as tf

    titles = "; ".join(str(i.get("title") or "") for i in built)
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
        *[_fix_lines(item) + "\n" for item in built],
    ])
    try:
        task = _clickup("POST", f"/list/{tf.AGENT_FIXES_LIST}/task", {
            "name": f"[Feature request] {titles}"[:180],
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
    # Items share a pull request now, so it is the urls that are counted.
    return len({
        item.get("pr_url") or id(item)
        for r in rows or [] for item in _items(dict(r)).values()
        if item.get("pr_day") == today and not item.get("sam_approved")
    })


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Logging for Sam
# ---------------------------------------------------------------------------

def _log(ts: str, row: dict, items: dict, n: str) -> str:
    """A build that fell through, with no word from Sam. A linked task
    stays where it is (Sam's Priority Queue) and gets the analysis once the
    thread settles (_record). With no linked task nothing is created: a
    client ask is Sam's to place, and an unasked task is noise."""
    item = items[n]
    if row.get("task_id"):
        item["state"] = LOGGED
        return (f"*{n}* stays on the linked task in your Priority Queue. "
                f"{_place_hint(n)}")
    item["state"] = WAITING_SAM
    return f"<@{SAM_SLACK}> {_place_hint(n)}"


def _place_hint(n: str) -> str:
    return (f"`build {n}` moves it to Sam Build Now, `log {n}` to Raw "
            f"Intake, `route {n} onlyg` to an engineer.")


# ---------------------------------------------------------------------------
# Placing it: Sam says where the request goes (2026-10-21)
# ---------------------------------------------------------------------------
#
# Sam Build Now   Sam builds it himself, from the requester's own spec. The
#                 task moves there, assigned to him, high priority. No
#                 analysis is added: he works from Ron's prompt.
# Raw Intake      logged for later: the task moves there, Sam unassigned,
#                 the analysis on it for whoever picks it up.
# Priority Queue  left where it is, nothing written.
# An engineer     the task moves to the Priority Queue, assigned to the
#                 engineer alone (Sam removed), a ping in their channel.
#
# Ron has usually filed a task already, so it is moved, never copied. A task
# is created only when the post links none.

class PlacementError(RuntimeError):
    """Nothing was moved. The message is safe to post."""


def _place(ts: str, row: dict, items: dict, command: dict) -> dict:
    verb = command["verb"]
    targets = [n for n, _ in command["targets"]]
    built = [n for n in targets
             if items[n].get("state") in (BUILDING, PR_OPEN)]
    targets = [n for n in targets if n not in built]
    notes = [f"{', '.join(built)} already has a pull request, so it stays "
             "as built."] if built else []
    engineer = {}
    if verb == "route":
        engineer = _engineer(command.get("engineer") or "")
        if not engineer:
            _post("Route it to whom? `route N onlyg` or `route N sanjay`.",
                  thread_ts=ts)
            return {"status": "ok", "action": "asked which engineer"}
    if not targets:
        _post(" ".join(notes), thread_ts=ts)
        return {"status": "ok", "action": "nothing to place"}

    task_id = command.get("task") or row.get("task_id") or ""
    try:
        where, created = _put(ts, row, items, targets, verb, engineer,
                              task_id)
    except PlacementError as exc:
        _post(f":warning: Nothing moved: {exc}", thread_ts=ts)
        return {"status": "error", "reason": str(exc)}

    state = {"build": BUILD_NOW, "log": LOGGED, "leave": LEFT,
             "route": ROUTED}[verb]
    for n in targets:
        items[n].update(state=state, placed=where, recorded=True)
        if created.get(n):
            items[n]["clickup"] = created[n]
        elif task_id and task_id != row.get("task_id"):
            items[n]["clickup"] = task_id
    _save(ts, items=json.dumps(items))
    settles = (all(i.get("state") in FINAL for i in items.values())
               and row.get("status") != SETTLED)
    if not settles or notes:
        _post(" ".join([*([] if settles else
                          [f"{_list(command['targets'])}: {where}."]),
                        *notes]), thread_ts=ts)
    _record(ts, row, items)
    return {"status": "ok", "action": verb, "where": where}


def _put(ts: str, row: dict, items: dict, targets: list, verb: str,
         engineer: dict, task_id: str) -> tuple[str, dict]:
    """The ClickUp half: where it landed, and any task created per item."""
    import clickup_search

    if task_id:
        try:
            task = _clickup("GET", f"/task/{task_id}")
        except Exception as exc:
            raise PlacementError(f"I couldn't read task {task_id} ({exc})")
        home = str((task.get("list") or {}).get("id") or "")
        names = {v: k for k, v in clickup_search.LISTS.items()}
        if home not in names:
            raise PlacementError(
                f"task {task_id} is in {(task.get('list') or {}).get('name')}"
                ", not a product list, so I left it alone")
        if verb == "leave":
            return f"left in {names[home]}", {}
        url = f"<https://app.clickup.com/t/{task_id}|task>"
        if verb == "build":
            _move(task_id, home, SAM_ACCEPTED, "Sam Build Now")
            _update(task_id, {"assignees": {"add": [SAM_CLICKUP], "rem": []},
                              "priority": 2})
            link = _permalink(ts)
            _comment(task_id, "Sam is building this himself (Sam Build Now"
                              f", from #feature-requests). {link}".strip())
            where = f"moved to Sam Build Now, assigned to you, high ({url})"
        elif verb == "log":
            _move(task_id, home, RAW_INTAKE, "Raw Intake")
            _update(task_id, {"assignees": {"add": [],
                                            "rem": [SAM_CLICKUP]}})
            for n in targets:
                if items[n].get("analysis"):
                    _comment(task_id, _item_comment(items[n], ts))
            where = f"moved to Raw Intake for later ({url})"
        else:
            _move(task_id, home, PRIORITY_QUEUE, "the Priority Queue")
            status = str((task.get("status") or {}).get("status") or "")
            body = {"assignees": {"add": [engineer["clickup"]],
                                  "rem": [SAM_CLICKUP]}}
            if status.lower() not in ENGINEER_OPEN:
                body["status"] = "queued"
            _update(task_id, body)
            for n in targets:
                if items[n].get("analysis"):
                    _comment(task_id, _item_comment(items[n], ts))
            _tell_engineer(ts, row, items[targets[0]], engineer, task_id)
            where = (f"routed to {engineer['name']} in the Priority Queue "
                     f"and posted in <#{engineer['channel']}> ({url})")
        _set_source(task_id)
        return where, {}

    if verb == "leave":
        return "left here, nothing filed", {}
    created = {}
    for n in targets:
        if verb == "build":
            new = _create_task(items[n], ts, row, list_id=SAM_ACCEPTED,
                               assignees=[SAM_CLICKUP], priority=2)
        elif verb == "log":
            new = _create_task(items[n], ts, row)
        else:
            new = _create_task(items[n], ts, row, list_id=PRIORITY_QUEUE,
                               assignees=[engineer["clickup"]],
                               status="queued")
            if new:
                _tell_engineer(ts, row, items[n], engineer, new)
        if not new:
            raise PlacementError("the ClickUp task could not be created")
        created[n] = new
    where = {"build": "added to Sam Build Now, assigned to you, high",
             "log": "added to Raw Intake for later",
             "route": f"routed to {engineer.get('name')} in the Priority "
                      f"Queue and posted in <#{engineer.get('channel')}>"
             }[verb]
    links = ", ".join(f"<https://app.clickup.com/t/{t}|task>"
                      for t in created.values())
    return f"{where} ({links})", created


def _move(task_id: str, home: str, list_id: str, name: str) -> None:
    import clickup_tasks

    if home != list_id and not clickup_tasks.move_clickup_task(task_id,
                                                               list_id):
        raise PlacementError(f"ClickUp refused the move to {name}. Move "
                             f"task {task_id} by hand")


def _update(task_id: str, body: dict) -> None:
    try:
        _clickup("PUT", f"/task/{task_id}", body)
    except Exception as exc:
        print(f"[FEATURE DESK] update of {task_id} failed: {exc}")


def _engineer(name: str) -> dict:
    import sprint_thread

    key = sprint_thread.ENGINEER_ALIASES.get(name, name)
    if key not in sprint_thread.ENGINEERS:
        return {}
    clickup_id, slack_id, _team, channel = sprint_thread.ENGINEERS[key]
    return {"key": key, "clickup": clickup_id, "slack": slack_id,
            "channel": channel,
            "name": {"onlyg": "OnlyG", "sanjay": "Sanjay"}.get(key, key)}


def _tell_engineer(ts: str, row: dict, item: dict, engineer: dict,
                   task_id: str) -> None:
    link = _permalink(ts)
    _post("\n".join(part for part in (
        f"<@{engineer['slack']}> Routed to you from #feature-requests "
        f"({row.get('requester') or 'a teammate'}): "
        f"*{item.get('title') or 'Request'}*",
        f"Spec and analysis on ClickUp: https://app.clickup.com/t/{task_id}",
        f"Thread: {link}" if link else "",
    ) if part), channel=engineer["channel"])


def _record(ts: str, row: dict, items: dict, summary: str = "") -> None:
    """Where the thread landed, on the linked task, once. Nothing is
    written while any item is still open: until then Slack is the
    conversation and the outcome can still change (Sam, 2026-10-19)."""
    task_id = row.get("task_id") or ""
    if not items or any(
            i.get("state") not in FINAL for i in items.values()):
        return
    fresh = {n: i for n, i in items.items() if not i.get("recorded")}
    if task_id and fresh:
        _comment(task_id, _comment_text(summary, fresh, ts))
        _set_source(task_id)
        for item in fresh.values():
            item["recorded"] = True
        _save(ts, items=json.dumps(items))
    _settle(ts, row, items)


def _settle(ts: str, row: dict, items: dict) -> None:
    """Once per thread: a check mark on the post and one closing line with
    the ClickUp link. Slack is the conversation, ClickUp the record."""
    if (_row(ts).get("status") or "") in (SETTLED, CLOSED):
        return
    _save(ts, status=SETTLED)
    _react(ts)
    _post(_settled_line(row, items), thread_ts=ts)


_LANDED = {PR_OPEN: "PR open for Sam to merge",
           LOGGED: "left in your Priority Queue", DECLINED: "not building",
           EXISTS: "already there", FRONTEND: "frontend only"}


def _landed(item: dict) -> str:
    state = str(item.get("state"))
    if state in (BUILD_NOW, LEFT, ROUTED) or (state == LOGGED
                                              and item.get("placed")):
        # "moved to Sam Build Now, assigned to you, high (<link|task>)":
        # the link is already in the closing line.
        return str(item.get("placed") or "").split(" (<")[0]
    if state == LOGGED and item.get("clickup"):
        return "logged in Raw Intake"
    return _LANDED.get(state, state)


def _settled_line(row: dict, items: dict) -> str:
    landed = ", ".join(
        f"{n} {_landed(i)}"
        for n, i in sorted(items.items(), key=lambda kv: int(kv[0])))
    links = []
    if row.get("task_id"):
        links.append(f"<https://app.clickup.com/t/{row['task_id']}|task>")
    links += [f"<https://app.clickup.com/t/{i['clickup']}|task>"
              for i in items.values() if i.get("clickup")]
    links += [f"<https://app.clickup.com/t/{i['fix_task']}|Agent Fixes>"
              for i in items.values() if i.get("fix_task")]
    where = (f"Recorded on ClickUp: {', '.join(links)}." if links
             else "Nothing needed filing on ClickUp.")
    return f":{DONE_EMOJI}: Handled: {landed}. {where}"


def _react(ts: str) -> None:
    try:
        _slack().reactions_add(channel=CHANNEL, timestamp=ts,
                               name=DONE_EMOJI)
    except Exception as exc:
        if "already_reacted" not in str(exc):
            print(f"[FEATURE DESK] check mark not added on {ts}: {exc}")


def _create_task(item: dict, ts: str, row: dict, list_id: str = RAW_INTAKE,
                 **extra) -> str:
    """Only for a post that links no task. Build now carries the request
    as written, since Sam works from the requester's words, and so does a
    request placed without an analysis."""
    if list_id == SAM_ACCEPTED or not item.get("analysis"):
        link = _permalink(ts)
        body = f"{_read_post(ts).get('text') or ''}\n\nSlack: {link}"
    else:
        body = _item_comment(item, ts)
    try:
        task = _clickup("POST", f"/list/{list_id}/task", {
            "name": str(item.get("title") or "Feature request")[:180],
            "markdown_description": body,
            "custom_fields": [{"id": FIELD_SOURCE, "value": SOURCE_SLACK}],
            **extra,
        })
        return str(task.get("id") or "")
    except Exception as exc:
        print(f"[FEATURE DESK] task not created in {list_id} for {ts}: "
              f"{exc}")
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


def _comment_text(summary: str, items: dict, ts: str) -> str:
    parts = [f"**Feature desk outcome:** {summary}".strip()]
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
# After the PR: the release watcher reports back into the thread
# ---------------------------------------------------------------------------
#
# Sam, 2026-10-19: once a desk PR merges, tell the requester in the same
# thread so they can test on development. The release watcher already finds
# merged agent PRs (ticket_fix.promote_merged) and proves fixes live, so it
# calls these. Each says one thing once, per item.

def _items_with(value: str) -> list[tuple[str, dict, dict]]:
    """(ts, row, items) for every post whose items mention `value` (a PR
    url or an Agent Fixes task id)."""
    if not value or not store.ensure_table(LEDGER):
        return []
    rows = store.query(f"SELECT * FROM {LEDGER} WHERE items LIKE :v",
                       {"v": f"%{value}%"})
    return [(r["ts"], dict(r), _items(dict(r))) for r in rows or []]


def _requester_tag(ts: str) -> str:
    try:
        who = _read_post(ts).get("requester_id") or ""
    except Exception:
        who = ""
    return f"<@{who}> " if who else ""


def pr_merged(pr_url: str, commit: str) -> list[str]:
    """The PR merged into the dev branch: the requester can test it. One
    message per thread, naming every item the PR carried."""
    out = []
    for ts, row, items in _items_with(pr_url):
        ns = [n for n, item in items.items()
              if item.get("pr_url") == pr_url and not item.get("merged")]
        if not ns:
            continue
        for n in ns:
            items[n]["merged"] = commit
        _save(ts, items=json.dumps(items))
        _post(f":test_tube: *{', '.join(ns)}. Merged into development* "
              f"({commit}). {_requester_tag(ts)}it's ready to test on dev. "
              "I'll post here again when it's live in production.",
              thread_ts=ts)
        out.append(f"feature desk {ts} item {', '.join(ns)} merged")
    return out


def fix_live(fix_task_id: str) -> list[str]:
    """The release watcher proved the merged commit live in production."""
    out = []
    for ts, row, items in _items_with(fix_task_id):
        ns = [n for n, item in items.items()
              if item.get("fix_task") == fix_task_id
              and not item.get("live")]
        if not ns:
            continue
        for n in ns:
            items[n]["live"] = True
        _save(ts, items=json.dumps(items))
        _post(f":rocket: *{', '.join(ns)}. Live in production.* "
              f"{_requester_tag(ts)}it's out for everyone now.",
              thread_ts=ts)
        out.append(f"feature desk {ts} item {', '.join(ns)} live")
    return out


def pr_closed(pr_url: str) -> list[str]:
    """The PR was closed without merging. Its items are not shipping as
    built, so they go back to Sam and the thread loses its check mark."""
    out = []
    for ts, row, items in _items_with(pr_url):
        ns = [n for n, item in items.items() if item.get("pr_url") == pr_url]
        if not ns:
            continue
        for n in ns:
            items[n].update(state=HELD, closed_pr=pr_url, pr_url="",
                            pr_branch="", recorded=False)
        _save(ts, items=json.dumps(items), status=OPEN)
        _unreact(ts)
        _post(f"*{', '.join(ns)}*: the pull request was closed without "
              f"merging, so this isn't shipping as built. <@{SAM_SLACK}> "
              f"`go {' '.join(ns)}` builds it again, or "
              f"`log {' '.join(ns)}`.", thread_ts=ts)
        out.append(f"feature desk {ts} item {', '.join(ns)} PR closed")
    return out


def _unreact(ts: str) -> None:
    try:
        _slack().reactions_remove(channel=CHANNEL, timestamp=ts,
                                  name=DONE_EMOJI)
    except Exception as exc:
        if "no_reaction" not in str(exc):
            print(f"[FEATURE DESK] check mark not removed on {ts}: {exc}")


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
             if not m.get("bot_id")
             and m.get("subtype") in (None, "file_share")
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
