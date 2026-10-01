"""
sentry_projects.py

Which Sentry project is which repository, and which of them we triage.

WHY THIS FILE EXISTS, AND WHY IT IS NOT A MODEL'S JOB
-----------------------------------------------------
The first cut of this pipeline left "which repo does this issue belong to" to
the triage agent, with the project slug as a hint. That is wrong. The mapping
from a Sentry project to a repository is a fixed fact about our
infrastructure, known in advance, and identical every time. Asking a model to
re-derive a constant on every issue is slower, costs money, and is
occasionally wrong, which is the worst of the three.

So it is a table. The agents get told which repository they are looking at,
and spend their reasoning on the actual bug.

THE THING THAT IS NOT OBVIOUS FROM THE SENTRY UI
------------------------------------------------
We separate dev from prod BY PROJECT, not by the `environment` tag. There are
five projects and they come in pairs: `dev-vome-app` and `prod-vome` are the
same codebase, as are `dev-volunteer-database` and `prod-volunteer-database`.

That matters because the obvious production filter, "drop anything whose
environment is not production", does almost nothing here: an `issue.created`
payload carries no environment at all, and when one does arrive it is as
likely to say `production` on a dev project as anything else. The real
production filter is this table. The environment rule in `sentry_gate.py` is
kept as a second line, not the first.

WHAT IS NOT COVERED
-------------------
Three repositories have no Sentry project at all, so their errors are
invisible to this pipeline and to Sentry generally. That is a gap in
instrumentation, not in this file, and no amount of work here closes it.

  * `vomeadmin/vome-react`, the admin web client. This is the surprising one
    and the most worth fixing: it is the interface our customers spend their
    day in, and the only frontend Sentry project we have is the mobile app.
    A customer reporting "the schedule page went blank" leaves no trace
    anywhere today.
  * `vomedjango-chats-app`
  * `vomedjango-integrations-app`

AN UNMAPPED PROJECT IS A CONFIGURATION GAP, NOT NOISE
-----------------------------------------------------
A project slug that is not in this table is dropped, and the daily report
names it in its own section. A new Sentry project appearing and being silently
ignored forever is exactly the failure this pipeline is supposed to prevent,
so it is made loud rather than convenient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Hosts. The backend repositories are on Bitbucket, the frontend ones on
# GitHub, which is why `code_search.py` carries a connector for each.
BITBUCKET = "bitbucket"
GITHUB = "github"

# Stacks. Decides which engineer reads it and which Slack channel it lands in.
BACKEND = "backend"
FRONTEND = "frontend"
MOBILE = "mobile"


@dataclass(frozen=True)
class Project:
    """One Sentry project and the repository behind it."""

    slug: str
    repo: str
    host: str
    owner: str
    stack: str
    production: bool
    # The environment variable naming the Slack channel for this stack, not
    # the channel itself, so channel ids stay in the environment.
    channel_env: str
    # The branch whose code produced these errors.
    #
    # This is per project, not per repository, and that is the whole point:
    # `dev-vome-app` and `prod-vome` are the same repo at different refs.
    # Reading a traceback from the dev deployment against `main` means
    # reasoning about code that is not running, and the line numbers in the
    # stack will not even match. Every read of a file or a commit list has to
    # carry this ref.
    ref: str = "main"
    notes: str = ""

    @property
    def qualified_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    def channel(self) -> str:
        return os.environ.get(self.channel_env, "")


# ---------------------------------------------------------------------------
# The table. Five Sentry projects, three repositories.
# ---------------------------------------------------------------------------

_PROJECTS: tuple[Project, ...] = (
    # django-core. The largest by far: 3.5K errors against 126K transactions
    # at the time of writing, so it sets the volume for the whole pipeline.
    Project(
        slug="prod-vome",
        repo="vomedjango-restored-core-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=True,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        # Production is `master`, confirmed by Sam on 2026-10-01. NOT `main`,
        # even though git reports origin/HEAD -> origin/main: the default
        # branch pointer and the branch that is actually deployed are two
        # different things, and only the second one matters here.
        ref="master",
    ),
    Project(
        slug="dev-vome-app",
        repo="vomedjango-restored-core-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        ref="development",
        notes=(
            "Same repo as prod-vome, different branch. The first project "
            "switched on, via SENTRY_PROJECT_ALLOWLIST."
        ),
    ),
    # django-db, the volunteer database service.
    Project(
        slug="prod-volunteer-database",
        repo="vomedjango-database-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=True,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        # UNCONFIRMED. Set to match the core repo's convention, but this repo
        # has `main`, `master` AND `prod-master`, so it is a guess between
        # three. Confirm before this project is ever triaged. It is not today,
        # so a wrong value here cannot currently affect anything.
        ref="master",
    ),
    Project(
        slug="dev-volunteer-database",
        repo="vomedjango-database-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        ref="development",
        notes="Same repo as prod-volunteer-database, different branch.",
    ),
    # The mobile app, React Native.
    #
    # Confirmed from an event's App context: build `com.vomeinc.vomemobile`,
    # build type `app store`, with device, foreground state and memory usage.
    # Those contexts only exist on a native app, so this is VomeApp and not
    # the vome-react web client.
    #
    # Note the owner. This repo is on a personal account rather than the
    # `vomeadmin` organization that holds vome-react, which is why
    # VOMEOS_GITHUB_OWNER must be `samfagen15` and not the org.
    Project(
        slug="vome-2j",
        repo="VomeApp",
        host=GITHUB,
        owner="samfagen15",
        stack=MOBILE,
        production=True,
        # Sanjay covers frontend for both web and mobile, so mobile lands in
        # the frontend channel rather than needing one of its own.
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        # VomeApp's default branch is master, not main.
        ref="master",
        notes="React Native. Crash reports, not browser errors.",
    ),
)

BY_SLUG = {project.slug: project for project in _PROJECTS}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def route(slug: str) -> Project | None:
    """The project behind a Sentry slug, or None if it is not mapped."""
    return BY_SLUG.get((slug or "").strip())


def all_projects() -> tuple[Project, ...]:
    return _PROJECTS


def _triage_dev() -> bool:
    """Whether the dev projects enter the pipeline.

    Off by default. A dev project is where a bug is supposed to appear, and
    paging anyone about one defeats the purpose.
    """
    return os.environ.get("SENTRY_TRIAGE_DEV_PROJECTS", "").lower() == "true"


def triaged_slugs() -> tuple[str, ...]:
    """Projects that enter the pipeline.

    `SENTRY_PROJECT_ALLOWLIST` overrides the table entirely, for narrowing
    the pipeline to one project while tuning it. Otherwise the production
    projects are in and the dev ones are out.
    """
    override = tuple(
        value.strip()
        for value in os.environ.get("SENTRY_PROJECT_ALLOWLIST", "").split(",")
        if value.strip()
    )
    if override:
        return override
    if _triage_dev():
        return tuple(project.slug for project in _PROJECTS)
    return tuple(
        project.slug for project in _PROJECTS if project.production
    )


def repos_in_scope() -> tuple[str, ...]:
    """Repository slugs the triaged projects point at.

    This is what `VOMEOS_CODE_REPOS` must contain for the analyst to be able
    to read the code behind an issue. Printed by the health check so the two
    cannot silently disagree.
    """
    return tuple(
        sorted(
            {
                project.repo
                for project in _PROJECTS
                if project.slug in triaged_slugs()
            }
        )
    )


def describe() -> dict[str, object]:
    """The routing table, for the health check and the CLI."""
    triaged = triaged_slugs()
    return {
        "projects": [
            {
                "slug": p.slug,
                "repo": p.qualified_repo,
                "ref": p.ref,
                "host": p.host,
                "stack": p.stack,
                "production": p.production,
                "triaged": p.slug in triaged,
                "channel_set": bool(p.channel()),
                "notes": p.notes,
            }
            for p in _PROJECTS
        ],
        "triaged_slugs": list(triaged),
        "repos_in_scope": list(repos_in_scope()),
        "uninstrumented_repos": [
            # The admin web client. No Sentry project, so browser errors are
            # invisible. The most worth fixing of the three.
            "vomeadmin/vome-react",
            "vomedjango/vomedjango-chats-app",
            "vomedjango/vomedjango-integrations-app",
        ],
        "github_owner_required": "samfagen15",
    }
