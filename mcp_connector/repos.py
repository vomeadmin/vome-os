"""
mcp_connector/repos.py

Which repository, on which host, and which branch is LIVE.

The live branch is the whole point. A customer asking how something works
needs "what are they running", and several of our repositories keep a default
branch that is nothing of the kind:

  * core, db, chats: `main` is a 2024 "Initial commit" and nothing else.
    Production is `master`, which the Bitbucket pipelines deploy.
  * vome-react, landing page: default `develop` / `main`; the GitHub Actions
    `cicd-prod.yml` deploys `master`.

Reading a default branch there answers about an empty repository or about
unreleased work, and does it with total confidence.

WHY THIS IS ITS OWN TABLE, NOT sentry_projects.py
--------------------------------------------------
It started as a view over `sentry_projects.py`, which only covers repos with
a Sentry project and states no evidence. Verifying branch by branch on
2026-10-15 found VomeApp's `master` was a March 2023 commit (v2.0.4) while the
store build lived on `sanjay_expo_upgrade`. Sam promoted that branch to
`master` the same day, so both tables now agree, but the near miss is why each
row here records how its branch was confirmed.

Every ref can be overridden without a deploy:

    MCP_LIVE_BRANCHES   comma separated repo=branch pairs, e.g.
                        VomeApp=Ron_Mobile_App_Prod-Level
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Repo:
    slug: str
    host: str  # "bitbucket" | "github"
    owner: str
    live_ref: str
    what: str
    evidence: str


_REPOS = (
    Repo("vomedjango-restored-core-app", "bitbucket", "vomedjango", "master",
         "core backend (django-core)",
         "bitbucket-pipelines.yml deploys master; main is a 2024 stub"),
    Repo("vomedjango-database-app", "bitbucket", "vomedjango", "master",
         "volunteer database backend (django-db)",
         "bitbucket-pipelines.yml deploys master; main is a 2024 stub"),
    Repo("vomedjango-chats-app", "bitbucket", "vomedjango", "master",
         "chat service backend",
         "development PRs merge into master; main is a 2024 stub. Prod "
         "deploy is manual, the pipeline only auto-deploys development"),
    Repo("vomedjango-integrations-app", "bitbucket", "vomedjango", "main",
         "integrations backend (Blackbaud, SSO, webhooks)",
         "the only release branch: development PRs merge into main. Prod "
         "deploy is manual, the pipeline only auto-deploys development"),
    Repo("vomesalesforce", "bitbucket", "vomedjango", "main",
         "Salesforce managed package (Apex, metadata)",
         "single long-lived branch, last synced from the org 2026-02-19"),
    Repo("vome-react", "github", "vomeadmin", "master",
         "web app frontend",
         ".github/workflows/cicd-prod.yml deploys master"),
    Repo("vome-landing-page", "github", "vomeadmin", "master",
         "public marketing site",
         ".github/workflows/cicd-prod.yml deploys master"),
    Repo("VomeApp", "github", "samfagen15", "master",
         "mobile app (iOS and Android)",
         "the store build (sanjay_expo_upgrade, v3.6.6) was promoted to "
         "master on 2026-10-15; master's tree is identical to it"),
)

ALIASES = {
    "core": "vomedjango-restored-core-app",
    "backend": "vomedjango-restored-core-app",
    "django-core": "vomedjango-restored-core-app",
    "database": "vomedjango-database-app",
    "db": "vomedjango-database-app",
    "django-db": "vomedjango-database-app",
    "chats": "vomedjango-chats-app",
    "chat": "vomedjango-chats-app",
    "integrations": "vomedjango-integrations-app",
    "salesforce": "vomesalesforce",
    "sf": "vomesalesforce",
    "web": "vome-react",
    "frontend": "vome-react",
    "react": "vome-react",
    "landing": "vome-landing-page",
    "landing-page": "vome-landing-page",
    "website": "vome-landing-page",
    "marketing": "vome-landing-page",
    "mobile": "VomeApp",
    "app": "VomeApp",
}


def _overrides() -> dict[str, str]:
    raw = os.environ.get("MCP_LIVE_BRANCHES", "")
    pairs = {}
    for item in raw.split(","):
        repo, _, ref = item.partition("=")
        if repo.strip() and ref.strip():
            pairs[repo.strip().lower()] = ref.strip()
    return pairs


def all_repos() -> tuple[Repo, ...]:
    overrides = _overrides()
    out = []
    for repo in _REPOS:
        ref = overrides.get(repo.slug.lower())
        if ref and ref != repo.live_ref:
            repo = Repo(repo.slug, repo.host, repo.owner, ref, repo.what,
                        "set by MCP_LIVE_BRANCHES")
        out.append(repo)
    return tuple(out)


def slugs() -> tuple[str, ...]:
    return tuple(repo.slug for repo in all_repos())


def resolve(name: str) -> Repo | None:
    """A repo by slug or alias, case-insensitive."""
    wanted = (name or "").strip()
    wanted = ALIASES.get(wanted.lower(), wanted)
    for repo in all_repos():
        if repo.slug.lower() == wanted.lower():
            return repo
    return None
