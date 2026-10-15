"""
mcp_connector/repos.py

Which repository, on which host, and which branch is LIVE.

The live branch is the whole point. vome-react's default branch is `develop`
and production runs `master`; django-core's default is `main` and production
runs `master`. Reading the default branch answers "what is the team building",
and a customer asking how something works needs "what are they running".
Telling a customer about a button that only exists on `develop` is the
"fixed / on the roadmap with no record" failure from CLAUDE.md rule 3 in a
different coat.

So the refs come from `sentry_projects.py`, where Sam confirmed each
production branch on 2026-10-02, rather than being restated here. The two
services Sentry does not cover are listed below with their default branch.
"""

from __future__ import annotations

from dataclasses import dataclass

import sentry_projects


@dataclass(frozen=True)
class Repo:
    slug: str
    host: str  # "bitbucket" | "github"
    owner: str
    live_ref: str
    what: str


# Not in sentry_projects because neither has a Sentry project. Their default
# branch is their deployed branch.
_EXTRA = (
    Repo("vomedjango-chats-app", "bitbucket", "vomedjango", "main",
         "chat service backend"),
    Repo("vomedjango-integrations-app", "bitbucket", "vomedjango", "main",
         "integrations backend (Blackbaud, Salesforce, SSO, webhooks)"),
)

_WHAT = {
    "vomedjango-restored-core-app": "core backend (django-core)",
    "vomedjango-database-app": "volunteer database backend (django-db)",
    "vome-react": "web app frontend",
    "VomeApp": "mobile app",
}

ALIASES = {
    "core": "vomedjango-restored-core-app",
    "backend": "vomedjango-restored-core-app",
    "django-core": "vomedjango-restored-core-app",
    "database": "vomedjango-database-app",
    "db": "vomedjango-database-app",
    "django-db": "vomedjango-database-app",
    "web": "vome-react",
    "frontend": "vome-react",
    "react": "vome-react",
    "mobile": "VomeApp",
    "app": "VomeApp",
    "chats": "vomedjango-chats-app",
    "chat": "vomedjango-chats-app",
    "integrations": "vomedjango-integrations-app",
}


def all_repos() -> tuple[Repo, ...]:
    found: dict[str, Repo] = {}
    for project in sentry_projects.all_projects():
        if not project.production or project.repo in found:
            continue
        found[project.repo] = Repo(
            slug=project.repo,
            host=project.host,
            owner=project.owner,
            live_ref=project.ref,
            what=_WHAT.get(project.repo, project.stack),
        )
    for extra in _EXTRA:
        found.setdefault(extra.slug, extra)
    return tuple(found.values())


def resolve(name: str) -> Repo | None:
    """A repo by slug or alias, case-insensitive."""
    wanted = (name or "").strip()
    wanted = ALIASES.get(wanted.lower(), wanted)
    for repo in all_repos():
        if repo.slug.lower() == wanted.lower():
            return repo
    return None
