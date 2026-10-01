"""
code_access_monitor.py

Watch the credentials the Sentry pipeline depends on, and say something only
when one is broken or about to be.

WHY THIS EXISTS
---------------
Same reason as `clickup_webhook_monitor.py`, and the same failure shape that
cost most of a day on 2026-09-29: an expired or revoked token is completely
silent. No failed deploy, no exception, no alert. The analyst simply reports
"could not reach the repository" on every issue, which reads like a bad bug
rather than a dead credential, and the pipeline keeps running and producing
nothing.

Every token here expires. Atlassian API tokens with scopes cap out at 365
days and GitHub's fine-grained tokens do the same, so this is not a question
of whether it breaks, only when. A year is exactly long enough for everyone
to have forgotten the token exists.

SILENT WHEN HEALTHY
-------------------
It posts on failure, on approaching expiry, and never otherwise. A monitor
that reports every healthy pass gets muted, and a muted monitor is the same
as no monitor. That line is lifted from the ClickUp monitor because it was
right there.

NOT CONFIGURED IS NOT A FAILURE
-------------------------------
Phase 1 does not read code, so the Bitbucket and GitHub tokens are legitimately
absent today. An absent token is reported as `skipped` and says nothing. A
monitor that complains every morning about a thing you have not built yet is
one you learn to ignore before it ever tells you something true.

HOW EXPIRY IS KNOWN
-------------------
GitHub volunteers it: every response to a fine-grained token carries
`github-authentication-token-expiration`. Bitbucket does not expose it at all,
so it has to be written down when the token is made, in
VOMEOS_BITBUCKET_TOKEN_EXPIRES. An unset date is reported as unknown rather
than assumed safe, because "we cannot tell" and "it is fine" are different
answers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sentry_notify
import sentry_projects
from vomeos.integrations import code_search
from vomeos.integrations import sentry as sentry_api

# How long before expiry to start saying something. Thirty days is enough to
# rotate a token that needs an org admin to approve it.
WARN_DAYS = int(os.environ.get("CODE_ACCESS_EXPIRY_WARN_DAYS", "30"))

OK = "ok"
SKIPPED = "skipped"
WARNING = "warning"
FAILING = "failing"


@dataclass
class Check:
    """One credential's health."""

    name: str
    status: str
    detail: str = ""
    expires_at: datetime | None = field(default=None)
    days_left: int | None = field(default=None)

    @property
    def speaks(self) -> bool:
        """Whether this check has anything worth interrupting someone for."""
        return self.status in (WARNING, FAILING)


def _parse_date(value: str) -> datetime | None:
    """Parse the date formats these two APIs actually hand back."""
    value = (value or "").strip()
    if not value:
        return None
    # GitHub: "2026-12-31 00:00:00 +0000" or "2026-12-31 00:00:00 UTC"
    cleaned = value.replace("UTC", "+0000").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _days_until(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    return (moment - datetime.now(timezone.utc)).days


def _expiry_verdict(name: str, expires_at: datetime | None,
                    working: bool, detail: str) -> Check:
    """Fold a working credential's expiry date into its status."""
    days = _days_until(expires_at)
    if not working:
        return Check(name, FAILING, detail, expires_at, days)
    if days is not None and days <= WARN_DAYS:
        return Check(
            name, WARNING,
            f"expires in {days} day(s), on {expires_at:%Y-%m-%d}",
            expires_at, days,
        )
    return Check(name, OK, detail, expires_at, days)


# ---------------------------------------------------------------------------
# The individual checks
# ---------------------------------------------------------------------------

def check_bitbucket() -> Check:
    client = code_search.Bitbucket()
    if not client.configured():
        return Check("bitbucket", SKIPPED, "no token set")

    repos = code_search.allowed_repos()
    repo = repos[0] if repos else "vomedjango-restored-core-app"
    result = client.read_file(repo, "manage.py", ref="development")

    expires_at = _parse_date(
        os.environ.get("VOMEOS_BITBUCKET_TOKEN_EXPIRES", "")
    )
    if not result.ok:
        hint = ""
        if result.status_code == 401:
            hint = (
                " Try VOMEOS_BITBUCKET_EMAIL to switch to Basic auth, or the"
                " token has expired."
            )
        return Check(
            "bitbucket", FAILING,
            f"cannot read {repo}: {result.error[:120]}{hint}",
            expires_at, _days_until(expires_at),
        )

    detail = f"reading {repo} ok"
    if expires_at is None:
        detail += ". Expiry unknown, set VOMEOS_BITBUCKET_TOKEN_EXPIRES"
    return _expiry_verdict("bitbucket", expires_at, True, detail)


def _github_repo_for(owner: str) -> str:
    """A repo under this owner to read as the health probe.

    Taken from the project routing table rather than hardcoded, so adding a
    GitHub project does not also mean editing this file.
    """
    for project in sentry_projects.all_projects():
        if (
            project.host == sentry_projects.GITHUB
            and project.owner.lower() == owner.lower()
        ):
            return project.repo
    return ""


def check_github_owner(owner: str) -> Check:
    """One GitHub account's token.

    Per owner, not per connector, because a fine-grained token is scoped to
    a single account at creation and cannot be changed afterwards. Two
    accounts means two tokens with two independent expiry dates, and one
    dying must not be hidden by the other working.
    """
    name = f"github:{owner}"
    client = code_search.GitHub()
    if not client.configured(owner):
        return Check(name, SKIPPED, f"no token set for {owner}")

    repo = _github_repo_for(owner)
    if not repo:
        return Check(name, SKIPPED, f"no repo mapped to {owner}")

    probe = "package.json"
    result = client.read_file(repo, probe, owner=owner)

    # GitHub hands the expiry back on every response, including failures.
    expires_at = _parse_date(
        result.headers.get("github-authentication-token-expiration", "")
    )

    if not result.ok:
        hint = ""
        if result.status_code == 404:
            hint = (
                " A 404 here is usually the wrong token rather than a"
                f" missing file: a fine-grained token scoped to another"
                f" account answers 404, not 403. Check"
                f" {client.token_env_name(owner)}."
            )
        return Check(
            name, FAILING,
            f"cannot read {owner}/{repo}: {result.error[:110]}{hint}",
            expires_at, _days_until(expires_at),
        )

    return _expiry_verdict(
        name, expires_at, True, f"reading {owner}/{repo} ok"
    )


def check_github() -> list[Check]:
    """Every GitHub account we hold a token for."""
    owners = code_search.github_owners_configured()
    if not owners:
        return [Check("github", SKIPPED, "no token set")]
    return [check_github_owner(owner) for owner in owners]


def check_sentry() -> Check:
    """The pipeline's own read token. Phase 3 cannot see a stack trace
    without it, and the webhook keeps arriving either way, so a dead token
    here produces confident analysis of nothing."""
    client = sentry_api.Sentry()
    if not client.configured():
        return Check("sentry", SKIPPED, "no auth token set")

    org = client.org
    result = client.list_projects()
    if not result.ok:
        return Check(
            "sentry", FAILING,
            f"cannot list projects for {org}: {result.error[:120]}",
        )
    count = len(result.data) if isinstance(result.data, list) else "?"
    return Check("sentry", OK, f"{count} project(s) visible in {org}")


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def _format(checks: list[Check]) -> str:
    speaking = [c for c in checks if c.speaks]
    failing = [c for c in speaking if c.status == FAILING]

    if failing:
        headline = "*Sentry pipeline: a credential is not working*"
    else:
        headline = "*Sentry pipeline: a credential is expiring*"

    # Sam, not the infra rota. These tokens are created under his Atlassian
    # and GitHub accounts, so he is the only person who can rotate them.
    # OnlyG is copied because a dead read token is why the analyst has gone
    # quiet, and he is the one who will notice that first.
    who = sentry_notify.Recipients(
        channel=sentry_notify.channel(),
        owners=("sam",),
        cc=("onlyg",),
        reason="credential rotation",
    )
    lines = [f"{who.mentions()} {headline}", ""]

    for check in speaking:
        marker = "BROKEN " if check.status == FAILING else "EXPIRING"
        lines.append(f"[{marker}] {check.name}: {check.detail}")

    healthy = [c.name for c in checks if c.status == OK]
    skipped = [c.name for c in checks if c.status == SKIPPED]
    if healthy:
        lines.append("")
        lines.append(f"Still fine: {', '.join(healthy)}")
    if skipped:
        lines.append(f"Not configured yet: {', '.join(skipped)}")

    if failing:
        lines.append("")
        lines.append(
            "Until this is fixed the analyst cannot read code, and every "
            "issue it reports will say so. The pipeline keeps accepting "
            "webhooks either way."
        )
    return "\n".join(lines)


def _post(text: str) -> bool:
    channel = sentry_notify.channel()
    try:
        from slack import client

        client.chat_postMessage(channel=channel, text=text)
        return True
    except Exception as exc:
        print(f"[CODE-ACCESS] could not post to Slack: {exc}")
        print(text)
        return False


def check_code_access() -> dict:
    """Run every credential check. Post only if something needs a human."""
    checks = [check_bitbucket(), *check_github(), check_sentry()]

    for check in checks:
        print(
            f"[CODE-ACCESS] {check.name}: {check.status}"
            + (f" ({check.detail})" if check.detail else "")
        )

    speaking = [c for c in checks if c.speaks]
    if not speaking:
        # Silent on purpose. A monitor that posts on every healthy pass gets
        # muted, and a muted monitor is the same as no monitor.
        return {
            "status": "healthy",
            "posted": False,
            "checks": {c.name: c.status for c in checks},
        }

    posted = _post(_format(checks))
    return {
        "status": "failing" if any(
            c.status == FAILING for c in speaking
        ) else "expiring",
        "posted": posted,
        "checks": {c.name: c.status for c in checks},
        "problems": {c.name: c.detail for c in speaking},
    }


def describe() -> dict:
    """Current credential health, for /sentry/status."""
    checks = [check_bitbucket(), *check_github(), check_sentry()]
    return {
        "warn_days": WARN_DAYS,
        "checks": {
            c.name: {
                "status": c.status,
                "detail": c.detail,
                "days_left": c.days_left,
            }
            for c in checks
        },
    }
