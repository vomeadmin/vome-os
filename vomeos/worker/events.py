"""
vomeos/worker/events.py

The event handler registry. What runs when something happens, rather than at
a time.

WHY THIS EXISTS
---------------
The worker layer shipped with two shapes of work: a scheduled job (`run_job`,
looked up in the job registry) and one agent run (`run_agent`). Neither fits a
webhook.

A webhook is a third shape. Something happened in another system, it has a
payload, and the work it triggers is an application function, not an agent.
Today every webhook in `main.py` handles that with `asyncio.to_thread`, which
runs the work inside the web process: a deploy kills it mid-flight, a slow
handler occupies the event loop, nothing is retried, and nothing is recorded.
That is the exact problem the worker layer was built to solve, and scheduled
jobs were simply the first case moved onto it.

So this is `schedule.py` for events. Same shape, same reasoning, same
registration-from-the-application direction:

    from vomeos.worker import register_event_handler

    register_event_handler(
        "engineering.sentry_issue",
        handle_sentry_issue,
        queue="engineering",
    )

The OS holds the registry and owns execution. The application owns what the
handlers do. The OS never imports the application, it imports the modules
named in VOMEOS_JOB_MODULES, exactly as it already does for jobs.

WHY NO CLAIM
------------
A scheduled job claims its period because "the daily digest" is a thing that
happens once a day and a redelivery would send it twice. An event has no
period. Its identity is whatever the source system calls it, which the OS
cannot know.

So idempotency is the handler's own, keyed on the source system's identifier,
and every handler registered here must have one. `sentry_handler` claims the
Sentry issue id in `vomeos_sentry_issues` before doing anything, which is the
same INSERT-ON-CONFLICT pattern `claim.py` uses, keyed on an entity instead of
a period.

A handler with no idempotency key is a bug, not a style choice. Celery
delivers at least once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class EventError(ValueError):
    """An event handler was registered with something unusable."""


@dataclass(frozen=True)
class EventHandler:
    """One thing that runs when something happens elsewhere."""

    key: str
    fn: Callable[[dict], object] = field(compare=False, repr=False)
    queue: str = "default"
    # Seconds. A handler still running after this is killed, so a wedged
    # webhook cannot hold a worker slot forever.
    time_limit: int = 600
    description: str = ""


_HANDLERS: dict[str, EventHandler] = {}


def register_event_handler(
    key: str,
    fn: Callable[[dict], object],
    *,
    queue: str = "default",
    time_limit: int = 600,
    description: str = "",
) -> EventHandler:
    """Register an event handler. Idempotent for the same key and callable.

    `key` is `<division>.<name>`, matching how jobs and agents are addressed,
    so one scoreboard can group all three the same way.
    """
    if "." not in key:
        raise EventError(
            f"event handler key {key!r} must be '<division>.<name>' so it "
            "can be routed and reported alongside jobs and agents"
        )
    if not callable(fn):
        raise EventError(f"event handler {key!r}: fn is not callable")

    existing = _HANDLERS.get(key)
    if existing is not None and existing.fn is not fn:
        raise EventError(
            f"event handler {key!r} is already registered to a different "
            "callable; two handlers cannot share a key"
        )

    handler = EventHandler(
        key=key,
        fn=fn,
        queue=queue,
        time_limit=time_limit,
        description=description,
    )
    _HANDLERS[key] = handler
    return handler


def get_event_handler(key: str) -> EventHandler:
    if key not in _HANDLERS:
        known = ", ".join(sorted(_HANDLERS)) or "(none)"
        raise EventError(
            f"no event handler registered as {key!r}. Known handlers: "
            f"{known}. Is the registering module listed in "
            "VOMEOS_JOB_MODULES?"
        )
    return _HANDLERS[key]


def list_event_handlers() -> list[EventHandler]:
    return sorted(_HANDLERS.values(), key=lambda h: h.key)


def queues() -> list[str]:
    """Every queue the registered handlers use."""
    return sorted({handler.queue for handler in _HANDLERS.values()})


def clear() -> None:
    """Empty the registry. For tests."""
    _HANDLERS.clear()
