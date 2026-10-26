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
We separate dev from prod BY PROJECT, not by the `environment` tag. Every
repository has a pair named after it, `<repo>-prod` and `<repo>-dev`, using
the names we use locally: django-core, django-db, django-chats,
django-integrations, vome-react, landing-page and vome-app. Sentry teams
group them by layer (backend, web, mobile).

The projects were renamed to that convention on 2026-10-23. Before then they
were `prod-vome`/`dev-vome-app` (core), `prod-volunteer-database`/
`dev-volunteer-database` (db), `prod-vome-web`/`dev-vome-web` (react) and
`vome-2j` (mobile, which is also the org slug). Those old slugs live on as
`aliases`: ledger rows written before the rename still carry them, and so
may `SENTRY_PROJECT_ALLOWLIST`, so `route()` and `triaged_slugs()` accept
either and always answer with the new one.

Count them from the API, not the dashboard. The project grid honours a "My
Teams" filter, which showed five of the seven and sent the first version of
this table out with the web client missing entirely. `find_projects` is the
inventory; a filtered dashboard is not.

That matters because the obvious production filter, "drop anything whose
environment is not production", does almost nothing here: an `issue.created`
payload carries no environment at all, and when one does arrive it is as
likely to say `production` on a dev project as anything else. The real
production filter is this table. The environment rule in `sentry_gate.py` is
kept as a second line, not the first.

WHAT IS NOT COVERED
-------------------
Every repository has its projects now, but a project with no events is not an
instrumented service, it is an empty box with a name on it, and it reads as
coverage on every dashboard. `RECEIVING_NOTHING` below lists the ones still
empty: vome-react (the SDK is in, the DSN is missing at build time), chats
and integrations (no SDK init yet), django-db dev and the mobile dev project.
vome-react matters most: it is the interface customers spend their day in,
and today a blank schedule page leaves no trace anywhere.

None of these are fixable here. They are SDK work in the services themselves.

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
    # Slugs this project had before the 2026-10-23 rename. Matched on the
    # way in, never sent out.
    aliases: tuple[str, ...] = ()

    @property
    def qualified_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    def channel(self) -> str:
        return os.environ.get(self.channel_env, "")


# ---------------------------------------------------------------------------
# The table. Fourteen Sentry projects, seven repositories, a pair each.
# ---------------------------------------------------------------------------

_PROJECTS: tuple[Project, ...] = (
    # django-core. The largest by far: 3.5K errors against 126K transactions
    # at the time of writing, so it sets the volume for the whole pipeline.
    Project(
        slug="django-core-prod",
        aliases=("prod-vome",),
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
        slug="django-core-dev",
        aliases=("dev-vome-app",),
        repo="vomedjango-restored-core-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        ref="development",
        notes=(
            "Same repo as django-core-prod, different branch. The first "
            "project switched on, via SENTRY_PROJECT_ALLOWLIST."
        ),
    ),
    # django-db, the volunteer database service.
    Project(
        slug="django-db-prod",
        aliases=("prod-volunteer-database",),
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
        slug="django-db-dev",
        aliases=("dev-volunteer-database",),
        repo="vomedjango-database-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        ref="development",
        notes="Same repo as django-db-prod, different branch.",
    ),
    # django-chats, the chat service. Projects created 2026-10-23, before the
    # service had any SDK init, so they stay empty until it does.
    Project(
        slug="django-chats-prod",
        repo="vomedjango-chats-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=True,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        # UNCONFIRMED. Production is deployed by hand, likely from `master`.
        ref="master",
    ),
    Project(
        slug="django-chats-dev",
        repo="vomedjango-chats-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        ref="development",
        notes="Same repo as django-chats-prod, different branch.",
    ),
    # django-integrations. Same story as chats: the SDK is pinned in its
    # requirements but never initialised.
    Project(
        slug="django-integrations-prod",
        repo="vomedjango-integrations-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=True,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        # UNCONFIRMED. The repo has `main` and no `master`.
        ref="main",
    ),
    Project(
        slug="django-integrations-dev",
        repo="vomedjango-integrations-app",
        host=BITBUCKET,
        owner="vomedjango",
        stack=BACKEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_BACKEND",
        ref="development",
        notes="Same repo as django-integrations-prod, different branch.",
    ),
    # The admin web client, vome-react. Instrumented deliberately: the repo
    # has @sentry/react, a sourcemap upload step in its build, and an init in
    # src/utils/sentry.js.
    #
    # NOT YET RECEIVING ANYTHING as of 2026-10-02: zero issues and zero spans
    # in both projects. Zero SPANS is the telling part, because a browser SDK
    # sends performance transactions whether or not anything breaks, so this
    # is a dormant SDK rather than a quiet fortnight.
    #
    # The cause is almost certainly the DSN. `src/utils/sentry.js` reads
    # `process.env.REACT_APP_SENTRY_DSN` and no-ops without it, and Create
    # React App inlines REACT_APP_* at BUILD time, so setting it on the host
    # at runtime never reaches the bundle.
    #
    # Mapped and triaged anyway, so the day the DSN lands the routing already
    # works and nobody has to remember this file exists.
    Project(
        slug="vome-react-prod",
        aliases=("prod-vome-web",),
        repo="vome-react",
        host=GITHUB,
        owner="vomeadmin",
        stack=FRONTEND,
        production=True,
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        # CONFIRMED by Sam on 2026-10-02. Worth having asked: this repo
        # carries `develop` as its DEFAULT branch plus `master`,
        # `ProductionEnv` and `RealProdEnv`, so the deployed branch and the
        # default branch are different and only the deployed one is useful
        # here. Reading the default would have produced confident analysis
        # of code nobody is running.
        ref="master",
        notes=(
            "No events yet. REACT_APP_SENTRY_DSN is probably missing at "
            "build time; CRA inlines it, so a runtime variable does nothing."
        ),
    ),
    Project(
        slug="vome-react-dev",
        aliases=("dev-vome-web",),
        repo="vome-react",
        host=GITHUB,
        owner="vomeadmin",
        stack=FRONTEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        # `develop`, not `development`. This repo's default branch really is
        # spelled differently from the backend repos, which is exactly the
        # kind of thing a per-project ref field exists to hold.
        ref="develop",
        notes="No events yet. Same build-time DSN cause as vome-react-prod.",
    ),
    # The marketing site, Astro, static on S3 and CloudFront. Projects created
    # 2026-10-23 for broken scripts and failed contact form submissions.
    Project(
        slug="landing-page-prod",
        repo="vome-landing-page",
        host=GITHUB,
        owner="vomeadmin",
        stack=FRONTEND,
        production=True,
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        # cicd-prod.yml deploys on push to `master`.
        ref="master",
    ),
    Project(
        slug="landing-page-dev",
        repo="vome-landing-page",
        host=GITHUB,
        owner="vomeadmin",
        stack=FRONTEND,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        ref="develop",
        notes="Same repo as landing-page-prod, different branch.",
    ),
    # The mobile app, React Native.
    #
    # Confirmed from an event's App context: build `com.vomeinc.vomemobile`,
    # build type `app store`, with device, foreground state and memory usage.
    # Those contexts only exist on a native app, so this is VomeApp and not
    # the vome-react web client.
    #
    # Moved from the samfagen15 personal account to `vomeadmin` on
    # 2026-10-18, so one token now reaches it and vome-react alike.
    Project(
        slug="vome-app-prod",
        aliases=("vome-2j",),
        repo="VomeApp",
        host=GITHUB,
        owner="vomeadmin",
        stack=MOBILE,
        production=True,
        # Sanjay covers frontend for both web and mobile, so mobile lands in
        # the frontend channel rather than needing one of its own.
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        # VomeApp's default branch is master, not main.
        ref="master",
        notes="React Native. Crash reports, not browser errors.",
    ),
    # Created 2026-10-23. Until then dev builds reported into the prod
    # project under environment `development`.
    Project(
        slug="vome-app-dev",
        repo="VomeApp",
        host=GITHUB,
        owner="vomeadmin",
        stack=MOBILE,
        production=False,
        channel_env="SLACK_CHANNEL_ENG_FRONTEND",
        ref="development",
        notes="Same repo as vome-app-prod, different branch.",
    ),
)

# Projects that exist but receive nothing. Worse than missing, because they
# look like coverage on every dashboard. Checked against the API 2026-10-23.
# Take a slug off once a deliberate test error from that service lands.
RECEIVING_NOTHING = (
    "django-db-dev",
    "django-chats-prod",
    "django-chats-dev",
    "django-integrations-prod",
    "django-integrations-dev",
    "vome-react-prod",
    "vome-react-dev",
    "landing-page-prod",
    "landing-page-dev",
    "vome-app-dev",
)

BY_SLUG = {project.slug: project for project in _PROJECTS}
_BY_ANY_SLUG = {
    slug: project
    for project in _PROJECTS
    for slug in (project.slug, *project.aliases)
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def route(slug: str) -> Project | None:
    """The project behind a Sentry slug, current or pre-rename, or None."""
    return _BY_ANY_SLUG.get((slug or "").strip())


def canonical(slug: str) -> str:
    """The current slug for `slug`, or `slug` itself if it is not mapped."""
    project = route(slug)
    return project.slug if project else (slug or "").strip()


def with_aliases(slugs) -> tuple[str, ...]:
    """`slugs` plus every old name they had, for matching stored rows."""
    out: list[str] = []
    for slug in slugs:
        project = route(slug)
        for name in (project.slug, *project.aliases) if project else (slug,):
            if name not in out:
                out.append(name)
    return tuple(out)


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
    projects are in and the dev ones are out. Always current slugs: an old
    name in the allowlist is read as the project it now belongs to.
    """
    raw = os.environ.get("SENTRY_PROJECT_ALLOWLIST", "")
    override = tuple(
        dict.fromkeys(
            canonical(value) for value in raw.split(",") if value.strip()
        )
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


# ---------------------------------------------------------------------------
# Where a fix belongs, which is not where the error came from
# ---------------------------------------------------------------------------

# A production error is diagnosed against the branch that produced it, because
# that is the code which actually ran. The FIX does not belong there. Opening
# a pull request against `master` puts a proposal one click away from
# production and skips development and staging entirely, so every fix targets
# the integration branch instead, whatever project reported the bug.
#
# Per repository because the name is not consistent: the Django repos use
# `development`, vome-react uses `develop`.
#
# A repository that is absent is refused rather than defaulted: falling back
# to the error's own branch is exactly the behaviour this table exists to
# prevent, and it would be silent. VomeApp was absent until it grew a
# `development` branch; its fix for #9008 was refused on 2026-10-23 for that
# reason. Chats and integrations joined the same day, when triage started
# reading them.
_FIX_TARGETS = {
    "vomedjango-restored-core-app": "development",
    "vomedjango-database-app": "development",
    "vomedjango-chats-app": "development",
    "vomedjango-integrations-app": "development",
    "vome-react": "develop",
    "vome-landing-page": "develop",
    "VomeApp": "development",
}


def fix_target(repo: str) -> str:
    """The branch a pull request for `repo` must target, or "" if none.

    `VOMEOS_PR_TARGETS=repo:branch,repo:branch` overrides the table, so a
    repository that grows an integration branch can be wired up without a
    deploy. An empty string means no pull request may be opened for this
    repository at all.
    """
    raw = os.environ.get("VOMEOS_PR_TARGETS", "")
    for pair in raw.split(","):
        name, _, branch = pair.partition(":")
        if name.strip() == repo and branch.strip():
            return branch.strip()
    return _FIX_TARGETS.get(repo, "")


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
        "receiving_nothing": list(RECEIVING_NOTHING),
        "github_owners": sorted(
            {p.owner for p in _PROJECTS if p.host == GITHUB}
        ),
    }
