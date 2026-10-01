"""
vomeos/integrations/sentry.py

Read-only access to Sentry, plus the two pieces of Sentry's wire protocol the
OS has to know: how a webhook is signed, and what shape its payloads arrive
in.

WHY THE WEBHOOK PARSING LIVES HERE TOO
--------------------------------------
Everything else in this package is outbound. This module also holds the
inbound half, because signature verification and payload normalisation are
knowledge about how Sentry speaks, and splitting that across the kernel and
the application means two places to fix when Sentry changes a header name.
Both halves are pure stdlib plus httpx, so nothing about the boundary rule
changes.

WHY A CONNECTOR AT ALL, WHEN THE WEBHOOK CARRIES A PAYLOAD
----------------------------------------------------------
The webhook payload is thin. An `issue.created` hook gives you the issue's
title, culprit and counts, and no stack trace at all. Diagnosis needs the
latest event, which is a second call. Doing that here rather than in the
handler means one timeout, one result type, and one place where the token is
read.

THERE ARE NO WRITE OPERATIONS
-----------------------------
Same rule as `code_search.py`: resolving, ignoring, assigning and commenting
on an issue are all things a person does. The restraint is the absence of the
code, not an instruction in a charter that a model might reason around. If we
ever want an agent to mark an issue as resolved, that is a deliberate,
reviewed addition here and an approval in the calling agent's manifest.

ENVIRONMENT
-----------
    SENTRY_AUTH_TOKEN      read-only auth token (org:read, project:read,
                           event:read). VOMEOS_SENTRY_AUTH_TOKEN also read.
    SENTRY_ORG             organization slug
    SENTRY_BASE_URL        defaults to https://sentry.io/api/0
    SENTRY_WEBHOOK_SECRET  the Sentry app's client secret, for signatures
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult

# Resources we accept from a webhook. Anything else is a subscription we did
# not ask for, and it is rejected at the door rather than parsed.
SUPPORTED_RESOURCES = ("issue", "event_alert")

# Level ordering, lowest first. Used by the application's gate to express
# "error and above" without hardcoding the list in two places.
LEVELS = ("debug", "info", "warning", "error", "fatal")


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Inbound: signature verification
# ---------------------------------------------------------------------------

def verify_signature(raw_body: bytes, signature: str) -> bool:
    """Verify the `sentry-hook-signature` header against the raw body.

    HMAC-SHA256 of the exact bytes received, keyed with the Sentry app's
    client secret, compared in constant time.

    FAILS CLOSED when no secret is configured, which is deliberate and
    different from the Slack and Calendly helpers in this repository. Those
    predate any of this and return True so local development works. This
    endpoint feeds a pipeline that writes to a ledger and, in a later phase,
    opens pull requests, so an unsigned request must never reach it. An
    unconfigured secret means every call gets a 403, which is loud and
    immediate rather than silent.
    """
    secret = _env("SENTRY_WEBHOOK_SECRET", "VOMEOS_SENTRY_WEBHOOK_SECRET")
    if not secret:
        print("[SENTRY] no SENTRY_WEBHOOK_SECRET set, rejecting webhook")
        return False
    if not signature:
        return False
    expected = hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Inbound: payload normalisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IssueSignal:
    """One issue, in the shape the rest of the pipeline works in.

    Sentry sends two payload shapes that mean "look at this issue", and they
    disagree about nearly every field name. Normalising once here means the
    gate, the ledger and the agents never learn that there were two.
    """

    issue_id: str
    project: str = ""
    title: str = ""
    culprit: str = ""
    level: str = ""
    environment: str = ""
    platform: str = ""
    exception_type: str = ""
    # Sentry's issue category: "error" for a real exception, or a performance
    # category such as "db_query" for an N+1 or a slow query. Defaults to
    # "error" when absent, which is the safe reading: an unknown category
    # must never cause a real exception to be dropped.
    category: str = "error"
    times_seen: int = 0
    users_affected: int = 0
    permalink: str = ""
    # Why this arrived: "issue.created", "event_alert.triggered", and so on.
    reason: str = ""

    def short(self) -> str:
        return f"{self.project}#{self.issue_id} {self.title[:80]}"


def _first_tag(tags, key: str) -> str:
    """Pull one tag value out of either tag shape Sentry uses.

    An event's tags are a list of two-item lists. An issue's are a list of
    dicts. Both appear, depending on the hook.
    """
    if not isinstance(tags, (list, tuple)):
        return ""
    for tag in tags:
        if isinstance(tag, dict):
            if str(tag.get("key", "")) == key:
                return str(tag.get("value", "") or "")
        elif isinstance(tag, (list, tuple)) and len(tag) == 2:
            if str(tag[0]) == key:
                return str(tag[1] or "")
    return ""


def _category(payload: dict) -> str:
    """The issue's category, across the several names Sentry has used.

    A performance issue (an N+1 query, a slow DB call) arrives on the same
    webhook as an exception and is a completely different kind of work: real,
    worth fixing, and not a bug report. The gate needs to be able to tell
    them apart deliberately rather than by accident of level.

    Defaults to "error" when nothing says otherwise, because the cost of
    mislabelling a performance issue as an error is a wasted triage, and the
    cost of mislabelling an error as something else is a dropped bug.
    """
    for key in ("issueCategory", "issue_category", "issueType", "issue_type"):
        value = payload.get(key)
        if value:
            return str(value).lower()
    return "error"


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(payload: dict, resource: str = "") -> IssueSignal | None:
    """Turn a webhook body into an IssueSignal, or None if it is not one.

    None means "this is not something we asked for". The caller treats that
    as a quiet ignore, not an error: Sentry sends installation and comment
    hooks to the same URL.
    """
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action", "") or "")
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    resource = resource or ("issue" if "issue" in data else "")

    if resource == "issue" and isinstance(data.get("issue"), dict):
        return _from_issue(data["issue"], f"issue.{action or 'created'}")

    if resource == "event_alert" and isinstance(data.get("event"), dict):
        return _from_event(data["event"], f"event_alert.{action or 'triggered'}")

    # An issue body arriving under an unexpected resource header is still an
    # issue. Sentry has shipped more than one spelling of these hooks.
    if isinstance(data.get("issue"), dict):
        return _from_issue(data["issue"], f"issue.{action or 'created'}")
    if isinstance(data.get("event"), dict):
        return _from_event(data["event"], f"event_alert.{action or 'triggered'}")
    return None


def _from_issue(issue: dict, reason: str) -> IssueSignal | None:
    issue_id = str(issue.get("id", "") or "")
    if not issue_id:
        return None
    metadata = issue.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    project = issue.get("project")
    project_slug = ""
    if isinstance(project, dict):
        project_slug = str(project.get("slug", "") or "")
    elif isinstance(project, str):
        project_slug = project

    return IssueSignal(
        issue_id=issue_id,
        project=project_slug,
        title=str(issue.get("title", "") or ""),
        culprit=str(issue.get("culprit", "") or ""),
        level=str(issue.get("level", "") or "").lower(),
        category=_category(issue),
        # An issue payload carries no environment. See the gate for what the
        # pipeline does about that rather than guessing.
        environment="",
        platform=str(issue.get("platform", "") or ""),
        exception_type=str(metadata.get("type", "") or ""),
        times_seen=_int(issue.get("count")),
        users_affected=_int(issue.get("userCount")),
        permalink=str(issue.get("permalink", "") or ""),
        reason=reason,
    )


def _from_event(event: dict, reason: str) -> IssueSignal | None:
    issue_id = str(event.get("issue_id", "") or event.get("groupID", "") or "")
    if not issue_id:
        return None
    tags = event.get("tags")
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    return IssueSignal(
        issue_id=issue_id,
        project=str(event.get("project_slug", "") or ""),
        title=str(event.get("title", "") or metadata.get("type", "") or ""),
        culprit=str(event.get("culprit", "") or ""),
        level=str(
            event.get("level", "") or _first_tag(tags, "level") or ""
        ).lower(),
        category=_category(event),
        environment=str(
            event.get("environment", "")
            or _first_tag(tags, "environment")
            or ""
        ),
        platform=str(event.get("platform", "") or ""),
        exception_type=str(metadata.get("type", "") or ""),
        times_seen=0,
        users_affected=0,
        permalink=str(event.get("web_url", "") or ""),
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Outbound: the read connector
# ---------------------------------------------------------------------------

@dataclass
class Sentry(Connector):
    """Read-only Sentry access. No write operations exist."""

    system: str = "sentry"
    base_url: str = field(
        default_factory=lambda: _env("SENTRY_BASE_URL")
        or "https://sentry.io/api/0"
    )
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    @property
    def org(self) -> str:
        return _env("SENTRY_ORG", "VOMEOS_SENTRY_ORG")

    def configured(self) -> bool:
        return bool(
            _env("SENTRY_AUTH_TOKEN", "VOMEOS_SENTRY_AUTH_TOKEN")
            and self.base_url
        )

    def headers(self) -> dict[str, str]:
        token = _env("SENTRY_AUTH_TOKEN", "VOMEOS_SENTRY_AUTH_TOKEN")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    # -- read operations ------------------------------------------------

    def get_issue(self, issue_id: str) -> IntegrationResult:
        """The issue itself: title, culprit, counts, status, first and last
        seen. Everything the webhook gave us, plus what it did not."""
        return self._call("get_issue", "GET", f"/issues/{issue_id}/")

    def latest_event(self, issue_id: str) -> IntegrationResult:
        """The most recent event in the group.

        This is where the stack trace lives. The webhook does not carry one,
        so every diagnosis starts here.
        """
        return self._call(
            "latest_event", "GET", f"/issues/{issue_id}/events/latest/"
        )

    def issue_events(
        self, issue_id: str, limit: int = 10
    ) -> IntegrationResult:
        """Recent events in the group.

        For the questions one event cannot answer: is this one customer or
        many, one browser or all of them, one endpoint or the whole service.
        """
        return self._call(
            "issue_events",
            "GET",
            f"/issues/{issue_id}/events/",
            params={"limit": min(_int(limit, 10), 100)},
        )

    def issue_tags(self, issue_id: str) -> IntegrationResult:
        """Aggregated tag values across the group.

        The cheapest way to learn the blast radius: which environments,
        releases, browsers and servers this is happening on.
        """
        return self._call("issue_tags", "GET", f"/issues/{issue_id}/tags/")


def describe() -> dict[str, object]:
    """Whether Sentry is reachable, for the CLI and the health check."""
    sentry = Sentry()
    return {
        "sentry": "configured" if sentry.configured() else "not set",
        "org": sentry.org or "(not set)",
        "webhook_secret": (
            "configured"
            if _env("SENTRY_WEBHOOK_SECRET", "VOMEOS_SENTRY_WEBHOOK_SECRET")
            else "NOT SET (every webhook will be rejected)"
        ),
        "operations": [
            "get_issue",
            "latest_event",
            "issue_events",
            "issue_tags",
        ],
        "writes": "none available",
    }
