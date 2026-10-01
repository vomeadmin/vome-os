"""
sentry_gate.py

Stage 2 of the funnel: throw work away for free, before it costs a queue slot,
a model call or a person's attention.

WHY THE CHEAPEST STAGE MATTERS MOST
-----------------------------------
Every later stage costs something. The ledger costs a database round trip,
triage costs a model call, Slack costs the thing we have least of, which is
somebody's attention. This stage is pure Python over a dict and costs nothing
measurable, so every issue it drops here is one that never costs anything
anywhere.

It runs inside the web request, before the payload is queued, which is what
keeps a flood of staging noise from ever reaching a worker at all.

THE LIST WILL BE WRONG ON DAY ONE
---------------------------------
That is expected and it is why the list is data at the top of this file rather
than conditions buried in a function. Every entry should name the issue that
prompted it. The shadow-mode report counts what each rule dropped, so the list
grows from evidence: `sentry_triage` returning `noise` for the same exception
type three times is the argument for adding it here, where it is free, instead
of paying a model to keep saying so.

Be careful in the other direction too. A gate that silently eats everything
looks exactly like a gate that is working, right up until someone asks why no
issue has been filed in a month. That is the failure mode the ClickUp webhook
monitor exists for, and the same discipline applies: the shadow report prints
what was dropped and why, every day, so an over-eager rule is visible.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import sentry_projects
from vomeos.integrations.sentry import LEVELS, IssueSignal

# ---------------------------------------------------------------------------
# The rules, as data
# ---------------------------------------------------------------------------

DEFAULT_MIN_LEVEL = "error"


def min_level() -> str:
    """Anything below this never enters the pipeline.

    Warnings are a backlog, not an incident, and a pipeline that triages
    warnings will never get to the errors.

    Read at call time, like every other setting here, so the gate can be
    retuned by restarting a process rather than by shipping code.
    """
    return os.environ.get("SENTRY_MIN_LEVEL", DEFAULT_MIN_LEVEL).lower()


# Exception types that are noise by construction. Each entry names why,
# because an unexplained entry is one nobody will ever dare remove.
IGNORED_EXCEPTION_TYPES = {
    # The client went away mid-response. Nothing is broken on our side and
    # there is nothing to fix.
    "BrokenPipeError",
    "ConnectionResetError",
    "ClientDisconnected",
    "RemoteDisconnected",
    # Scanners and bots probing for URLs and vhosts that do not exist.
    "Http404",
    "DisallowedHost",
    "InvalidSessionKey",
    # A deploy replaced the bundle while someone had the old page open. Real,
    # but it is a deploy artefact rather than a bug, and it fires in bulk on
    # every release.
    "ChunkLoadError",
    # Cross-origin script errors carry no stack, no file and no line. There
    # is literally nothing to diagnose.
    "Script error.",
    # A rejected promise with a non-Error value. Sentry cannot group these
    # usefully and they are almost always third-party.
    "NonErrorPromiseRejection",
    "UnhandledRejection",
    # The browser aborted a fetch, usually because the user navigated away.
    "AbortError",
    # Famously meaningless. Fires on any page with a resize observer and
    # affects nothing a user can perceive.
    "ResizeObserverError",
}

# DELIBERATELY NOT IGNORED, and they were until the live data arrived:
# `SuspiciousOperation` and `SuspiciousFileOperation`. Both went on the list
# on the assumption they were scanner noise. That is true of DisallowedHost,
# a sibling subclass, and false of these two.
#
# The dev project had a real one within 48 hours: "SuspiciousFileOperation:
# Storage can not find an available filename" on an announcement PDF upload,
# which is a genuine filename-collision bug in our own attachment path. The
# rule would have eaten it silently. Django raises the bare parent for real
# bugs as well, so ignoring a parent class is how a noise list starts
# swallowing the thing it exists to surface.
#
# If the bare SuspiciousOperation does turn out to be noisy, add it back
# through SENTRY_IGNORE_EXCEPTION_TYPES, which needs no deploy. Evidence
# first, in both directions.

# Substrings matched against the issue title, for the errors whose type is
# generic and whose message is the identifying part.
IGNORED_TITLE_PATTERNS = (
    # Browser noise, for vome-react. Its two projects (dev-vome-web and
    # prod-vome-web) exist but receive nothing, so none of these fire yet.
    "ResizeObserver loop limit exceeded",
    "ResizeObserver loop completed with undelivered notifications",
    "Loading chunk",
    "Loading CSS chunk",
    "Failed to fetch dynamically imported module",
    "NetworkError when attempting to fetch resource",
    # Generic, both platforms.
    "Non-Error promise rejection captured",
    "The operation was aborted",
    # React Native and iOS connectivity. The phone lost signal, went through
    # a tunnel, or was backgrounded mid-request. This is the single largest
    # source of mobile noise and none of it is a bug in our code.
    #
    # The risk in ignoring these is that a real backend outage looks the same
    # from the handset. That is an acceptable trade here because the two
    # backends have their own Sentry projects and would light up first. If
    # the mobile app is ever the only instrumented client, revisit this.
    "Network request failed",
    "The Internet connection appears to be offline",
    "The network connection was lost",
    "The request timed out",
    "A server with the specified hostname could not be found",
    # Deliberately NOT here: the bare iOS "cancelled" message. These patterns
    # are substring matches and Vome's domain is full of cancellation, so it
    # would silently eat "Shift cancelled: AttributeError" and any real bug
    # in the cancellation paths. A noise rule that swallows real bugs in a
    # feature area is worse than the noise it removes.
)

# Culprit patterns that mean the code is not ours. A browser extension
# injecting a broken script into our page is not a bug we can fix.
IGNORED_CULPRIT_PATTERNS = tuple(
    re.compile(p, re.I)
    for p in (
        r"chrome-extension://",
        r"moz-extension://",
        r"safari-web-extension://",
        r"^anonymous$",
        r"/node_modules/",
        r"gtm\.js|googletagmanager|analytics\.js|fbevents\.js",
    )
)


def _csv_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def project_allowlist() -> tuple[str, ...]:
    """Sentry projects in scope, from the routing table.

    Never empty, and empty does NOT mean "everything". We run seven projects
    and they are all known in advance, so an unrecognised slug is a new
    project nobody has mapped yet, which is a configuration gap to report
    rather than traffic to accept.
    """
    return sentry_projects.triaged_slugs()


def allowed_environments() -> tuple[str, ...]:
    """Environments in scope. Defaults to production only."""
    return _csv_env("SENTRY_ENVIRONMENTS") or ("production",)


def _extra_ignored_types() -> set[str]:
    return set(_csv_env("SENTRY_IGNORE_EXCEPTION_TYPES"))


# Issue categories that enter the pipeline. Errors only, by default.
#
# Sentry sends performance issues (N+1 queries, slow DB calls, uncompressed
# assets) down the same webhook as exceptions, and on the live dev project
# they were four of seven issues in 48 hours. They are real and worth fixing
# and they are NOT bugs: the output of triaging one is "add select_related
# here", which belongs in an optimisation backlog rather than in a bug
# pipeline that can open a pull request.
#
# They were already being dropped, but by accident: their level is `info`, so
# the level rule caught them and reported them as a level drop. That is the
# wrong reason, it hides how many there are, and it would break the moment
# Sentry raised their level. Now they are dropped by name and counted
# separately, so "should we triage N+1 queries too" becomes an answerable
# question rather than an invisible one.
DEFAULT_CATEGORIES = ("error",)


def allowed_categories() -> tuple[str, ...]:
    """Issue categories in scope. Add db_query here to triage N+1 queries."""
    return _csv_env("SENTRY_ISSUE_CATEGORIES") or DEFAULT_CATEGORIES


@dataclass(frozen=True)
class Decision:
    """Whether an issue continues, and why not if it does not."""

    passed: bool
    # A short machine-readable rule name, so the shadow report can count by
    # rule rather than by free text.
    rule: str = ""
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return not self.passed


PASS = Decision(True)


def _level_rank(level: str) -> int:
    try:
        return LEVELS.index((level or "").lower())
    except ValueError:
        # An unrecognised level is treated as high, so a new Sentry level
        # name does not silently drop real errors.
        return len(LEVELS)


def check(signal: IssueSignal) -> Decision:
    """Should this issue continue into the pipeline?

    Order is cheapest and most selective first. Every branch names a rule so
    the daily report can say which one is doing the work.
    """
    # 1. Issue category. Before the level rule, because a performance issue
    # has level `info` and would otherwise be reported as a level drop, which
    # is true but useless: it hides the count behind the wrong reason.
    category = (signal.category or "error").lower()
    if category not in allowed_categories():
        return Decision(
            False, "issue_category", f"{category} is not triaged"
        )

    # 2. Level. The single most selective rule in normal operation.
    if _level_rank(signal.level) < _level_rank(min_level()):
        return Decision(
            False, "level", f"{signal.level or 'unset'} below {min_level()}"
        )

    # 3. Project. THIS is the production filter, not the environment rule
    # below. Dev and prod are separate Sentry projects here (prod-vome and
    # dev-vome-app are the same codebase), so the routing table decides what
    # is production and the environment tag is only a second line.
    known = sentry_projects.route(signal.project)
    if known is None:
        # Not a silent drop. An unmapped project means somebody created a
        # Sentry project and nobody told this pipeline, and the daily report
        # calls it out by name so it gets mapped rather than ignored forever.
        return Decision(
            False,
            "project_unmapped",
            f"{signal.project or 'unset'} is not in sentry_projects.py",
        )
    if signal.project not in project_allowlist():
        rule = "project_dev" if not known.production else "project_excluded"
        return Decision(
            False, rule, f"{signal.project} is not triaged"
        )

    # 4. Environment, the second line.
    #
    # An `issue.created` payload carries no environment field at all, so an
    # unknown environment CANNOT mean "drop it": that would silently discard
    # the entire pipeline's input and look identical to a quiet week. Rule 2
    # has already established that this is a production project, so unknown
    # here is both expected and safe.
    #
    # Set SENTRY_REQUIRE_ENVIRONMENT=true only if you later start sending
    # events (rather than issues) and want to filter within a project.
    environments = allowed_environments()
    if signal.environment:
        if signal.environment not in environments:
            return Decision(
                False, "environment", f"{signal.environment} not in scope"
            )
    elif os.environ.get("SENTRY_REQUIRE_ENVIRONMENT", "").lower() == "true":
        return Decision(False, "environment_unknown", "no environment on payload")

    # 5. Exception type.
    ignored_types = IGNORED_EXCEPTION_TYPES | _extra_ignored_types()
    if signal.exception_type and signal.exception_type in ignored_types:
        return Decision(
            False, "exception_type", f"{signal.exception_type} is on the list"
        )

    # 6. Title substrings, for the errors whose type is generic.
    title = signal.title or ""
    for pattern in IGNORED_TITLE_PATTERNS:
        if pattern.lower() in title.lower():
            return Decision(False, "title", f"matched {pattern!r}")

    # 7. Culprit. Code that is not ours.
    culprit = signal.culprit or ""
    if culprit:
        for pattern in IGNORED_CULPRIT_PATTERNS:
            if pattern.search(culprit):
                return Decision(
                    False, "culprit", f"matched {pattern.pattern!r}"
                )

    return PASS


def describe() -> dict[str, object]:
    """The gate's current configuration, for the health check and the CLI."""
    return {
        "min_level": min_level(),
        "categories": list(allowed_categories()),
        "projects": list(project_allowlist()),
        "environments": list(allowed_environments()),
        "require_environment": (
            os.environ.get("SENTRY_REQUIRE_ENVIRONMENT", "").lower() == "true"
        ),
        "ignored_exception_types": len(
            IGNORED_EXCEPTION_TYPES | _extra_ignored_types()
        ),
        "ignored_title_patterns": len(IGNORED_TITLE_PATTERNS),
        "ignored_culprit_patterns": len(IGNORED_CULPRIT_PATTERNS),
    }
