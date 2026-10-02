"""
sentry_event.py

Fetch one real occurrence of an issue and turn it into a stack trace a person
(or an agent) can read.

WHY THIS EXISTS
---------------
The webhook payload carries a title, a culprit and some counts. It does not
carry a single stack frame. Triage on a title alone is guessing: "AttributeError
on /api/forms/" could be a missing related name, a None that was never checked,
or a typo, and those are different bugs with different severities.

So before the triage agent runs, we fetch the latest event and render the
frames. That one extra API call is the difference between a verdict based on
evidence and a verdict based on a string.

REDACTION IS NOT OPTIONAL HERE
------------------------------
An event is the richest thing Sentry holds: request bodies, headers, cookies,
the logged-in user, and the local variables of every frame. All of it is about
to enter a model prompt. `sentry_redact.redact()` runs on the whole event
before a single character is read out of it, and the frame renderer only ever
sees the redacted copy.

WHAT GETS KEPT
--------------
In-app frames first, because a traceback through six layers of Django and one
line of ours is really about the one line of ours. Framework frames are kept
after them and only until the budget runs out, since "raised inside
django/db/models/query.py" is the difference between a missing row and a
broken connection.
"""

from __future__ import annotations

import os

import sentry_redact
from vomeos.integrations import sentry as sentry_api

# Frames to render. Deep enough to show how we got here, short enough that the
# fast tier is reading evidence rather than scrolling.
MAX_FRAMES = int(os.environ.get("SENTRY_TRACE_MAX_FRAMES", "18"))

# Hard cap on the rendered trace. A runaway recursion produces thousands of
# identical frames and would otherwise become the entire prompt.
MAX_TRACE_CHARS = int(os.environ.get("SENTRY_TRACE_MAX_CHARS", "6000"))


def _exception_values(event: dict) -> list[dict]:
    """The exception entries, across the two shapes Sentry returns.

    The REST API wraps everything in `entries`, a list of typed sections. A
    raw event has `exception` at the top level. Both turn up depending on
    which endpoint answered.
    """
    exception = event.get("exception")
    if isinstance(exception, dict) and isinstance(
        exception.get("values"), list
    ):
        return [v for v in exception["values"] if isinstance(v, dict)]

    entries = event.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "exception":
                continue
            data = entry.get("data")
            if isinstance(data, dict) and isinstance(data.get("values"), list):
                return [v for v in data["values"] if isinstance(v, dict)]
    return []


def _frames_of(value: dict) -> list[dict]:
    trace = value.get("stacktrace") or value.get("rawStacktrace")
    if isinstance(trace, dict) and isinstance(trace.get("frames"), list):
        return [f for f in trace["frames"] if isinstance(f, dict)]
    return []


def _render_frame(frame: dict) -> str:
    where = (
        frame.get("filename")
        or frame.get("absPath")
        or frame.get("module")
        or "<unknown>"
    )
    function = frame.get("function") or "<module>"
    lineno = frame.get("lineNo") or frame.get("lineno") or "?"
    line = f'File "{where}", line {lineno}, in {function}'

    # The source line itself, when Sentry captured context. This is often the
    # single most informative line in the whole trace.
    context = frame.get("context")
    if isinstance(context, list):
        for item in context:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and str(item[0]) == str(lineno)
            ):
                source = str(item[1] or "").strip()
                if source:
                    line += f"\n    {source}"
                break
    return line


def render_trace(event: dict) -> str:
    """Render a redacted event as a readable traceback.

    Returns an empty string when there is nothing to render, which the caller
    must treat as "no evidence" rather than "no problem".
    """
    values = _exception_values(event)
    if not values:
        return ""

    # The last value is the exception actually raised; earlier ones are the
    # chain it was raised from.
    value = values[-1]
    frames = _frames_of(value)

    # In-app first. A traceback through six layers of Django and one line of
    # ours is a bug about the one line of ours.
    in_app = [f for f in frames if f.get("inApp") or f.get("in_app")]
    others = [f for f in frames if f not in in_app]
    chosen = (in_app + others)[:MAX_FRAMES]

    lines = [_render_frame(f) for f in chosen]

    etype = value.get("type") or ""
    message = str(value.get("value") or "").strip()
    if etype or message:
        lines.append(f"{etype}: {message}" if etype else message)

    trace = "\n".join(lines)
    if len(trace) > MAX_TRACE_CHARS:
        trace = trace[:MAX_TRACE_CHARS] + "\n... trace truncated ..."
    return trace


def fetch_trace(issue_id: str) -> str:
    """Latest event for an issue, redacted and rendered. "" if unavailable.

    Never raises. A Sentry that is slow or a token that has expired must
    degrade triage to a title-only verdict rather than stopping the pipeline,
    and the caller can see the difference because the trace is empty.
    """
    client = sentry_api.Sentry()
    if not client.configured():
        return ""
    result = client.latest_event(issue_id)
    if not result.ok or not isinstance(result.data, dict):
        print(
            f"[SENTRY] no event for issue {issue_id}: "
            f"{result.error[:120] or 'unexpected payload'}"
        )
        return ""
    # Redact the whole event before reading one character out of it.
    return render_trace(sentry_redact.redact(result.data))
