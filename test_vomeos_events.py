"""
test_vomeos_events.py

The event handler registry: the third shape of work the OS runs.

The registry itself is small, so most of what is worth asserting is the
misuse. A handler registered under a key that is already taken, silently
replacing the first one, would be the kind of bug that only shows up as
"why did the Sentry pipeline stop doing anything".

The task's contract matters more than the registry's: an unknown key must not
retry (it is a configuration error and retrying just fills the log), and a
handler that raises must retry (it is probably a third party being briefly
slow).
"""

import pytest

from vomeos.worker import events


@pytest.fixture(autouse=True)
def clean_registry():
    events.clear()
    yield
    events.clear()


def _noop(payload):
    return {"seen": payload}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_a_handler_can_be_registered_and_found():
    events.register_event_handler("engineering.x", _noop, queue="engineering")
    handler = events.get_event_handler("engineering.x")
    assert handler.fn is _noop
    assert handler.queue == "engineering"


def test_registering_the_same_handler_twice_is_idempotent():
    # Module import is not guaranteed to happen once. register_all() in the
    # job modules is explicitly documented as safe to call again.
    events.register_event_handler("engineering.x", _noop)
    events.register_event_handler("engineering.x", _noop)
    assert len(events.list_event_handlers()) == 1


def test_two_different_handlers_cannot_share_a_key():
    # Silently replacing the first one is how a pipeline stops working with
    # no error anywhere.
    events.register_event_handler("engineering.x", _noop)
    with pytest.raises(events.EventError):
        events.register_event_handler("engineering.x", lambda p: None)


def test_a_key_must_name_a_division():
    # Same convention as jobs and agents, so one scoreboard can group all
    # three the same way.
    with pytest.raises(events.EventError):
        events.register_event_handler("sentry_issue", _noop)


def test_a_non_callable_is_refused():
    with pytest.raises(events.EventError):
        events.register_event_handler("engineering.x", "not a function")


def test_an_unknown_key_names_the_known_ones():
    # The error a person actually reads when VOMEOS_JOB_MODULES is wrong.
    events.register_event_handler("engineering.sentry_issue", _noop)
    with pytest.raises(events.EventError) as exc:
        events.get_event_handler("engineering.typo")
    assert "engineering.sentry_issue" in str(exc.value)
    assert "VOMEOS_JOB_MODULES" in str(exc.value)


def test_queues_reports_what_the_handlers_need():
    events.register_event_handler("engineering.a", _noop, queue="engineering")
    events.register_event_handler("support.b", _noop, queue="support")
    assert events.queues() == ["engineering", "support"]


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------

def test_the_task_runs_the_registered_handler():
    from vomeos.worker.tasks import run_event

    calls = []
    events.register_event_handler(
        "engineering.x", lambda p: calls.append(p) or {"ok": True}
    )
    result = run_event.run("engineering.x", {"issue": "1"})
    assert result["status"] == "ok"
    assert result["result"] == {"ok": True}
    assert calls == [{"issue": "1"}]


def test_an_unknown_handler_does_not_retry():
    # A configuration problem, not a transient one. Retrying it twice just
    # fills the log and delays the real signal.
    from vomeos.worker.tasks import run_event

    result = run_event.run("engineering.nope", {})
    assert result["status"] == "unknown_handler"


def test_a_handler_returning_a_non_dict_does_not_break_the_task():
    from vomeos.worker.tasks import run_event

    events.register_event_handler("engineering.x", lambda p: "just a string")
    assert run_event.run("engineering.x", {})["status"] == "ok"


def test_the_engineering_queue_is_declared_by_the_app():
    # The worker must declare the queue or beat sends to a queue nothing
    # consumes, which is silent.
    import engineering_jobs  # noqa: F401  registers on import

    from vomeos.worker.app import _queue_names

    assert "engineering" in _queue_names()
