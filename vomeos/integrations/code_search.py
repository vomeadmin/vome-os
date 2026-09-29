"""
vomeos/integrations/code_search.py

Read-only access to the product repositories, for diagnosis.

WHY OVER HTTP AND NOT A CLONE
-----------------------------
The obvious approach is to clone the repos onto the box and let an agent read
the filesystem. Three reasons not to:

  * Railway containers are ephemeral, so a clone is either re-fetched every
    boot or held on a volume that silently drifts from the real branch. An
    agent reasoning about stale code is worse than one with no code at all.
  * A clone is the whole repository. This is a URL per file, which means the
    access is enumerable and auditable.
  * Vome OS reaches every other system over the network and never by import
    or by disk. A clone would be the first exception, and exceptions are how
    that rule dies.

WHAT THIS CAN AND CANNOT DO
---------------------------
Four read operations: search code, read a file, list recent commits touching
a path, and read a commit. There are no write methods, at all. The access
control is not an instruction in a charter that a model might reason around;
it is the absence of any code that could write.

Use a token scoped to READ on the repositories you want reachable. If the
token can write, the connector's restraint is the only thing stopping a
mistake, and that is the wrong place for it to live.

THE HARD RULE ON WHAT THIS IS FOR
---------------------------------
Code context feeds internal notes, engineer handoffs and triage reasoning.
It must never reach a client. A reply that quotes a file path, a function
name, a branch or a commit tells a customer how our system is built and reads
as an internal note that escaped, which is precisely the ticket #8945 shape.

The enforcement is structural, not textual: the agents that draft client
messages do not get this connector, and the outbound guards run on everything
they produce. An agent's manifest is what grants a capability, so "who can
see the code" is a reviewable line in a file rather than a convention.

ENVIRONMENT
-----------
    VOMEOS_BITBUCKET_TOKEN     read-only app password or access token
    VOMEOS_BITBUCKET_WORKSPACE workspace slug (e.g. "vomedjango")
    VOMEOS_GITHUB_TOKEN        read-only fine-grained token
    VOMEOS_GITHUB_OWNER        org or user that owns the repos
    VOMEOS_CODE_REPOS          comma separated allowlist of repo slugs.
                               Empty means every repo the token can reach.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult

# How much of a file to hand back. A whole 4000 line module drowns the
# reasoning and costs more than it informs.
MAX_FILE_BYTES = int(os.environ.get("VOMEOS_CODE_MAX_FILE_BYTES", "60000"))


def allowed_repos() -> tuple[str, ...]:
    """The repositories an agent may reach, or () meaning no allowlist."""
    raw = os.environ.get("VOMEOS_CODE_REPOS", "")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


def _repo_permitted(repo: str) -> bool:
    allowed = allowed_repos()
    return not allowed or repo in allowed


@dataclass
class Bitbucket(Connector):
    """Read-only Bitbucket Cloud access.

    The backend repositories (django-core, django-chats, django-integrations)
    live here.
    """

    system: str = "bitbucket"
    base_url: str = "https://api.bitbucket.org/2.0"
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    @property
    def workspace(self) -> str:
        return os.environ.get("VOMEOS_BITBUCKET_WORKSPACE", "")

    def configured(self) -> bool:
        return bool(
            os.environ.get("VOMEOS_BITBUCKET_TOKEN") and self.workspace
        )

    def headers(self) -> dict[str, str]:
        token = os.environ.get("VOMEOS_BITBUCKET_TOKEN", "")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    # -- read operations ------------------------------------------------

    def search_code(self, query: str, repo: str = "") -> IntegrationResult:
        """Find where a symbol, string or error message appears."""
        if repo and not _repo_permitted(repo):
            return self._denied("search_code", repo)
        search = f"{query} repo:{repo}" if repo else query
        return self._call(
            "search_code",
            "GET",
            f"/workspaces/{self.workspace}/search/code",
            params={"search_query": search, "pagelen": 20},
        )

    def read_file(
        self, repo: str, path: str, ref: str = "HEAD"
    ) -> IntegrationResult:
        """One file at one ref. Truncated to MAX_FILE_BYTES."""
        if not _repo_permitted(repo):
            return self._denied("read_file", repo)
        result = self._call(
            "read_file",
            "GET",
            f"/repositories/{self.workspace}/{repo}/src/{ref}/{path}",
        )
        return _truncate(result)

    def recent_commits(
        self, repo: str, path: str = "", limit: int = 10
    ) -> IntegrationResult:
        """What changed here lately.

        The question behind most regression triage is "what shipped just
        before this started", and this is how to answer it without guessing.
        """
        if not _repo_permitted(repo):
            return self._denied("recent_commits", repo)
        suffix = f"/{path}" if path else ""
        return self._call(
            "recent_commits",
            "GET",
            f"/repositories/{self.workspace}/{repo}/commits{suffix}",
            params={"pagelen": min(int(limit), 50)},
        )

    def _denied(self, operation: str, repo: str) -> IntegrationResult:
        return IntegrationResult(
            self.system, operation, False,
            error=(
                f"repository {repo!r} is not in VOMEOS_CODE_REPOS; add it "
                "deliberately to make it reachable"
            ),
        )


@dataclass
class GitHub(Connector):
    """Read-only GitHub access. The OS repository itself lives here."""

    system: str = "github"
    base_url: str = "https://api.github.com"
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    @property
    def owner(self) -> str:
        return os.environ.get("VOMEOS_GITHUB_OWNER", "")

    def configured(self) -> bool:
        return bool(os.environ.get("VOMEOS_GITHUB_TOKEN") and self.owner)

    def headers(self) -> dict[str, str]:
        token = os.environ.get("VOMEOS_GITHUB_TOKEN", "")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def search_code(self, query: str, repo: str = "") -> IntegrationResult:
        if repo and not _repo_permitted(repo):
            return self._denied("search_code", repo)
        scope = f"repo:{self.owner}/{repo}" if repo else f"user:{self.owner}"
        return self._call(
            "search_code",
            "GET",
            "/search/code",
            params={"q": f"{query} {scope}", "per_page": 20},
        )

    def read_file(
        self, repo: str, path: str, ref: str = ""
    ) -> IntegrationResult:
        if not _repo_permitted(repo):
            return self._denied("read_file", repo)
        params = {"ref": ref} if ref else None
        result = self._call(
            "read_file",
            "GET",
            f"/repos/{self.owner}/{repo}/contents/{path}",
            params=params,
        )
        return _truncate(result)

    def recent_commits(
        self, repo: str, path: str = "", limit: int = 10
    ) -> IntegrationResult:
        if not _repo_permitted(repo):
            return self._denied("recent_commits", repo)
        params = {"per_page": min(int(limit), 50)}
        if path:
            params["path"] = path
        return self._call(
            "recent_commits",
            "GET",
            f"/repos/{self.owner}/{repo}/commits",
            params=params,
        )

    def _denied(self, operation: str, repo: str) -> IntegrationResult:
        return IntegrationResult(
            self.system, operation, False,
            error=(
                f"repository {repo!r} is not in VOMEOS_CODE_REPOS; add it "
                "deliberately to make it reachable"
            ),
        )


def _truncate(result: IntegrationResult) -> IntegrationResult:
    """Cap a file response so one large module cannot swamp the context."""
    if not result.ok or not isinstance(result.data, str):
        return result
    if len(result.data) <= MAX_FILE_BYTES:
        return result
    result.data = (
        result.data[:MAX_FILE_BYTES]
        + f"\n\n... truncated at {MAX_FILE_BYTES} bytes ..."
    )
    return result


def describe() -> dict[str, object]:
    """Which code sources are reachable, for the CLI and the health check."""
    bitbucket, github = Bitbucket(), GitHub()
    return {
        "bitbucket": "configured" if bitbucket.configured() else "not set",
        "github": "configured" if github.configured() else "not set",
        "allowlist": (
            list(allowed_repos()) or ["(no allowlist: all reachable)"]
        ),
        "operations": ["search_code", "read_file", "recent_commits"],
        "writes": "none available",
    }
