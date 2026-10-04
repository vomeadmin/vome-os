"""
sentry_notify.py

Where a Sentry issue gets posted, and who gets tagged on it.

ONE CHANNEL, DIFFERENT PEOPLE
-----------------------------
Everything goes to #eng-all. The routing is in the @mention, not the channel.

That is Sam's call and it is the right one for a team this size. Splitting
across #eng-backend and #eng-frontend sounds tidier and in practice means two
channels that are each too quiet to be worth opening, and an issue that
spans both is filed in whichever one the router guessed. One busy channel
that people actually read beats three correct ones they mute.

WHO GETS TAGGED
---------------
    backend          OnlyG
    frontend         Sanjay
    mobile           Sanjay
    infra or config  Siraj and OnlyG, with Sam copied

The first three come from the project's stack, which is a fixed fact in
`sentry_projects.py`. The fourth does not: an AWS or configuration problem
surfaces as an exception in a backend project like any other, so "is this
infra" is a property of the individual issue rather than of the project it
came from.

So there are two ways to reach it. `looks_like_infra()` is a deterministic
first pass over the exception type and culprit, and the triage agent can
override with `infra=True` once it exists. The deterministic pass is
deliberately narrow: tagging three people on a routine NoneType error is how
a channel gets muted, and a missed infra tag only costs one hop.

NOBODY IS TAGGED ON A DIGEST
----------------------------
Mentions are for the issues that interrupt. The daily report names people in
plain text and tags nobody, because a digest that pings four people every
morning at nine is a digest everyone filters.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import sentry_projects

# #eng-all. https://vomevolunteer.slack.com/archives/C0BR5RZ51B3
DEFAULT_CHANNEL = "C0BR5RZ51B3"

# Slack member IDs. Not secrets: they are visible to everyone in the
# workspace and are not credentials. Env overrides exist so a person can be
# changed without a deploy, which matters more than it sounds when somebody
# is on leave and the alerts are still tagging them.
_PEOPLE = {
    "onlyg": ("SLACK_USER_ONLYG", "U0APZ7JUHRD"),
    "sanjay": ("SLACK_USER_SANJAY", "U0AQJ8YJF6Y"),
    "siraj": ("SLACK_USER_SIRAJ", "U0ATX30F3TL"),
    "sam": ("SAM_SLACK_USER_ID", ""),
}


def channel() -> str:
    """The one channel every Sentry issue posts to.

    `SENTRY_AUTOMATIONS` is the name to set. It is read first because the
    whole point of #sentry-automations is that one variable moves everything
    this pipeline says, the daily report included, rather than three
    variables that can disagree and leave half the output somewhere else.

    The two older names still work so that setting this is not a flag day.
    """
    return (
        os.environ.get("SENTRY_AUTOMATIONS")
        or os.environ.get("SENTRY_SLACK_CHANNEL")
        or os.environ.get("SLACK_CHANNEL_ENG_ALERTS")
        or DEFAULT_CHANNEL
    )


def user_id(name: str) -> str:
    """One person's Slack member id, or empty if not configured."""
    env_name, fallback = _PEOPLE.get(name.lower(), ("", ""))
    if not env_name:
        return ""
    return os.environ.get(env_name) or fallback


def mention(name: str) -> str:
    """`<@U...>`, or the plain name when we have no id for them.

    Never returns an empty string. A message that silently drops the person
    it was meant to reach is worse than one that says "OnlyG" without
    pinging, because the second is visibly wrong.
    """
    uid = user_id(name)
    return f"<@{uid}>" if uid else name.title()


# ---------------------------------------------------------------------------
# Is this an infrastructure or configuration problem?
# ---------------------------------------------------------------------------

# Exception types that are almost always the platform rather than the code.
_INFRA_TYPES = frozenset(
    {
        "OperationalError",           # database unreachable, too many conns
        "InterfaceError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "EndpointConnectionError",    # botocore
        "ClientError",                # boto3, S3 and friends
        "NoCredentialsError",
        "PartialCredentialsError",
        "S3UploadFailedError",
        "SSLError",
        "ImproperlyConfigured",       # Django, a setting is wrong or missing
        "KeyError",                   # only when the culprit says settings
        "TimeoutError",
        "ReadTimeout",
        "ServiceUnavailable",
    }
)

# Culprits that place the failure in infrastructure or configuration.
_INFRA_CULPRITS = tuple(
    re.compile(p, re.I)
    for p in (
        r"boto3|botocore|s3transfer",
        r"\bsettings\b",
        r"redis|celery\.app|kombu",
        r"psycopg2|django\.db\.backends",
        r"storages\.backends",
        r"\bmigrations?\b",
    )
)


def looks_like_infra(exception_type: str = "", culprit: str = "") -> bool:
    """Deterministic first pass at "is this AWS or config".

    Narrow on purpose. `KeyError` is on the type list but only counts when
    the culprit also points at settings, because a bare KeyError is the most
    common exception in any Python codebase and tagging three people on every
    one would make this routing worthless within a week.
    """
    etype = (exception_type or "").strip()
    where = culprit or ""

    if etype == "KeyError":
        return bool(re.search(r"settings|environ|config", where, re.I))
    if etype in _INFRA_TYPES:
        return True
    return any(pattern.search(where) for pattern in _INFRA_CULPRITS)


# ---------------------------------------------------------------------------
# The routing decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recipients:
    """Who to tag, and why, for one issue."""

    channel: str
    # Tagged directly. They are expected to act.
    owners: tuple[str, ...] = ()
    # Copied in. Aware, not responsible.
    cc: tuple[str, ...] = field(default=())
    reason: str = ""

    def mentions(self) -> str:
        """The mention string to put at the top of a Slack message."""
        parts = [mention(name) for name in self.owners]
        if self.cc:
            parts.append(
                "cc " + " ".join(mention(name) for name in self.cc)
            )
        return " ".join(parts)


def route(
    project_slug: str = "",
    *,
    stack: str = "",
    exception_type: str = "",
    culprit: str = "",
    infra: bool | None = None,
) -> Recipients:
    """Who should see this issue.

    `infra` overrides the heuristic, for the triage agent to set once it
    exists. None means "work it out from the exception".
    """
    if not stack and project_slug:
        project = sentry_projects.route(project_slug)
        stack = project.stack if project else ""

    is_infra = (
        infra
        if infra is not None
        else looks_like_infra(exception_type, culprit)
    )

    if is_infra:
        return Recipients(
            channel=channel(),
            owners=("siraj", "onlyg"),
            cc=("sam",),
            reason="infrastructure or configuration",
        )

    if stack == sentry_projects.BACKEND:
        return Recipients(channel(), ("onlyg",), (), "backend")
    if stack in (sentry_projects.FRONTEND, sentry_projects.MOBILE):
        return Recipients(channel(), ("sanjay",), (), stack)

    # Unknown stack. Tag the backend lead rather than nobody: an issue that
    # reaches Slack with no owner is one everybody assumes is somebody
    # else's.
    return Recipients(channel(), ("onlyg",), (), "unrouted, defaulted")


def describe() -> dict[str, object]:
    """Routing configuration, for the health check."""
    return {
        "channel": channel(),
        "people": {
            name: (user_id(name) or "NOT SET") for name in _PEOPLE
        },
        "rules": {
            "backend": "onlyg",
            "frontend": "sanjay",
            "mobile": "sanjay",
            "infra_or_config": "siraj + onlyg, cc sam",
        },
    }
