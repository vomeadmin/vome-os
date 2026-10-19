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
Read operations only: search code, read a file, list recent commits touching
a path, ask whether one commit is an ancestor of another (compare /
merge_base), and list CI runs on a branch (workflow_runs / pipelines), which
is how the release watcher proves a fix is live. There are no write methods,
at all. The access control is not an instruction in a charter that a model
might reason around; it is the absence of any code that could write.

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
    VOMEOS_BITBUCKET_TOKEN     Atlassian API token with scopes. Only
                               read:repository:bitbucket is needed, and that
                               covers workspace code search too (verified
                               2026-10-01, 148 hits across the workspace).
    VOMEOS-PIPELINES           second Atlassian token carrying
    (or VOMEOS_BITBUCKET_PIPELINES_TOKEN)
                               read:pipeline:bitbucket, used only by
                               Bitbucket.pipelines(). Unset means the main
                               token is tried, which works if it has the scope.
                               The hyphenated name is the Railway one; some
                               shells drop hyphenated names, which is what the
                               underscore spelling is for.
    VOMEOS_BITBUCKET_WORKSPACE workspace slug (e.g. "vomedjango")
    VOMEOS_BITBUCKET_EMAIL     LEAVE UNSET. Only for app passwords and
                               unscoped tokens, which need Basic. See
                               Bitbucket.headers() for why Bearer is better.
    VOMEOS_GITHUB_TOKEN        shared fallback token
    vome-os-sentry-write-web   the vomeadmin token, read and write on every
                               GitHub repo. Wins over the per-owner variable;
                               see GITHUB_TOKEN_ALIASES.
    VOMEOS_GITHUB_TOKEN_<OWNER> per account, e.g.
                               VOMEOS_GITHUB_TOKEN_VOMEADMIN. A fine-grained
                               token is bound to one account, so a second
                               account would mean a second token.
    VOMEOS_GITHUB_OWNER        default owner when a caller names none
    VOMEOS_CODE_REPOS          comma separated allowlist of repo slugs.
                               Empty means every repo the token can reach.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult

# How much of a file to hand back. A whole 4000 line module drowns the
# reasoning and costs more than it informs.
MAX_FILE_BYTES = int(os.environ.get("VOMEOS_CODE_MAX_FILE_BYTES", "60000"))

# The Bitbucket token that may read pipelines, first match wins.
PIPELINES_TOKEN_VARS = ("VOMEOS-PIPELINES", "VOMEOS_BITBUCKET_PIPELINES_TOKEN")

# The one GitHub token per account, under the name it was created with in
# Railway. Every GitHub repo (vome-react, vome-landing-page, VomeApp) is under
# `vomeadmin` since 2026-10-18, and `vome-os-sentry-write-web` has read and
# write on all three, so reading and writing both use it. It is consulted
# BEFORE VOMEOS_GITHUB_TOKEN_<OWNER> so that the older read-only token still
# set in Railway is ignored rather than winning and then expiring.
GITHUB_TOKEN_ALIASES = {
    "VOMEADMIN": ("vome-os-sentry-write-web",),
}


def allowed_repos() -> tuple[str, ...]:
    """The repositories an agent may reach, or () meaning no allowlist."""
    raw = os.environ.get("VOMEOS_CODE_REPOS", "")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


def _repo_permitted(
    repo: str, allowlist: tuple[str, ...] | None = None
) -> bool:
    """`allowlist` is a caller's own list; None means VOMEOS_CODE_REPOS.

    Two callers want different reach. The Sentry bug analyst is autonomous
    and stays narrow on VOMEOS_CODE_REPOS. The claude.ai connector is people
    asking questions and passes its own list, so widening one never widens
    the other.
    """
    allowed = allowed_repos() if allowlist is None else allowlist
    return not allowed or repo in allowed


def _denial(repo: str, allowlist: tuple[str, ...] | None) -> str:
    # code_access_monitor matches "not in VOMEOS_CODE_REPOS" to tell a
    # deliberate refusal from a broken token, so that wording is a contract.
    source = "VOMEOS_CODE_REPOS" if allowlist is None else "MCP_CODE_REPOS"
    return (
        f"repository {repo!r} is not in {source}; add it deliberately to "
        "make it reachable"
    )


@dataclass
class Bitbucket(Connector):
    """Read-only Bitbucket Cloud access.

    The backend repositories (django-core, django-chats, django-integrations)
    live here.
    """

    system: str = "bitbucket"
    base_url: str = "https://api.bitbucket.org/2.0"
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)
    allowlist: tuple[str, ...] | None = None

    @property
    def workspace(self) -> str:
        return os.environ.get("VOMEOS_BITBUCKET_WORKSPACE", "")

    def configured(self) -> bool:
        return bool(
            os.environ.get("VOMEOS_BITBUCKET_TOKEN") and self.workspace
        )

    def headers(self) -> dict[str, str]:
        """Bearer by default, Basic when an email is configured.

        WHY BOTH
        --------
        Bitbucket has three credential shapes in circulation and they do not
        authenticate the same way:

          * Workspace / Repository Access Tokens: Bearer. Paid plans only.
          * Atlassian API token WITH scopes: Bearer. Any plan, created at
            id.atlassian.com rather than inside Bitbucket.
          * Atlassian API token WITHOUT scopes, and the old app passwords:
            HTTP Basic, with the Atlassian account email as the username.

        Bearer is the default and should stay the default. It does not
        care which Atlassian account owns the token, and that is the whole
        argument: on 2026-10-01 a Basic attempt with an email from a
        different account produced

            Token is invalid, expired, or not supported for this endpoint.

        every word of which was false. Bitbucket returns that one string for
        every credential problem, naming neither the half that was wrong nor
        the shape it wanted, so it sends you auditing scopes while the fault
        is an email address.

        Setting VOMEOS_BITBUCKET_EMAIL switches to Basic, for app passwords
        and unscoped tokens that genuinely need it. The email must then be
        the account that OWNS the token, not whoever is deploying.
        """
        return self._auth(os.environ.get("VOMEOS_BITBUCKET_TOKEN", ""))

    def _auth(self, token: str) -> dict[str, str]:
        email = os.environ.get("VOMEOS_BITBUCKET_EMAIL", "")
        if email:
            import base64

            pair = base64.b64encode(f"{email}:{token}".encode()).decode()
            return {
                "Accept": "application/json",
                "Authorization": f"Basic {pair}",
            }
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    # -- read operations ------------------------------------------------

    def search_code(self, query: str, repo: str = "") -> IntegrationResult:
        """Find where a symbol, string or error message appears."""
        if repo and not _repo_permitted(repo, self.allowlist):
            return self._denied("search_code", repo)
        search = f"{query} repo:{repo}" if repo else query
        return self._call(
            "search_code",
            "GET",
            f"/workspaces/{self.workspace}/search/code",
            params={"search_query": search, "pagelen": 20},
        )

    def read_file(
        self, repo: str, path: str, ref: str = "HEAD",
        max_bytes: int = 0,
    ) -> IntegrationResult:
        """One file at one ref. Truncated to MAX_FILE_BYTES.

        `max_bytes` raises the cap for a caller that windows the text
        itself, which a whole-file reader must not do.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("read_file", repo)
        result = self._call(
            "read_file",
            "GET",
            f"/repositories/{self.workspace}/{repo}/src/{ref}/{path}",
        )
        return _truncate(result, max_bytes)

    def recent_commits(
        self, repo: str, path: str = "", limit: int = 10, ref: str = ""
    ) -> IntegrationResult:
        """What changed here lately, on a named branch.

        The question behind most regression triage is "what shipped just
        before this started", and this is how to answer it without guessing.

        `ref` is not optional in practice. Bitbucket's bare `/commits`
        endpoint answers for the repository's main branch, so asking it about
        an error from the dev deployment returns the wrong history and looks
        entirely plausible doing it. The branch goes in the path as
        `/commits/{ref}`, and `path` becomes a query filter rather than a
        path segment, because the two cannot both be path segments.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("recent_commits", repo)
        params: dict = {"pagelen": min(int(limit), 50)}
        if path:
            params["path"] = path
        suffix = f"/{ref}" if ref else ""
        return self._call(
            "recent_commits",
            "GET",
            f"/repositories/{self.workspace}/{repo}/commits{suffix}",
            params=params,
        )

    def merge_base(self, repo: str, a: str, b: str) -> IntegrationResult:
        """The best common ancestor of two revisions.

        `merge_base(repo, sha, "master")` returning `sha` itself is how
        "is this commit on master" is spelled here: Bitbucket has no compare
        status like GitHub's, but a commit is an ancestor of a branch exactly
        when it is its own merge base with that branch.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("merge_base", repo)
        return self._call(
            "merge_base",
            "GET",
            f"/repositories/{self.workspace}/{repo}/merge-base/{a}..{b}",
        )

    def pull_request(self, repo: str, number: str) -> IntegrationResult:
        """One pull request: its state and, once merged, the merge commit.

        How an agent-opened fix PR is noticed reaching the dev branch, so the
        release watcher can take over from there.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("pull_request", repo)
        return self._call(
            "pull_request",
            "GET",
            f"/repositories/{self.workspace}/{repo}/pullrequests/{number}",
        )

    def pipelines(
        self, repo: str, branch: str, limit: int = 10
    ) -> IntegrationResult:
        """Recent pipelines on one branch, newest first.

        Needs read:pipeline:bitbucket, which the code-search token was never
        given. A separate pipelines token (PIPELINES_TOKEN_VARS) carries it,
        so adding CI visibility did not widen the token every agent reads
        code with.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("pipelines", repo)
        token = next(
            (os.environ[n] for n in PIPELINES_TOKEN_VARS if os.environ.get(n)),
            os.environ.get("VOMEOS_BITBUCKET_TOKEN", ""),
        )
        return self._call(
            "pipelines",
            "GET",
            f"/repositories/{self.workspace}/{repo}/pipelines/",
            params={
                "target.branch": branch,
                "sort": "-created_on",
                "pagelen": min(int(limit), 50),
            },
            headers=self._auth(token),
        )

    def _denied(self, operation: str, repo: str) -> IntegrationResult:
        return IntegrationResult(
            self.system, operation, False,
            error=_denial(repo, self.allowlist),
        )


@dataclass
class GitHub(Connector):
    """Read-only GitHub access. The OS repository itself lives here."""

    system: str = "github"
    base_url: str = "https://api.github.com"
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)
    allowlist: tuple[str, ...] | None = None

    @property
    def owner(self) -> str:
        """The default owner, used when a caller does not name one."""
        return os.environ.get("VOMEOS_GITHUB_OWNER", "")

    @staticmethod
    def token_env_name(owner: str) -> str:
        """The per-owner token variable: VOMEOS_GITHUB_TOKEN_<OWNER>."""
        slug = re.sub(r"[^A-Za-z0-9]", "_", owner or "").upper()
        return f"VOMEOS_GITHUB_TOKEN_{slug}" if slug else ""

    def token_for(self, owner: str = "") -> str:
        """The token that can actually reach this owner.

        WHY THIS IS PER OWNER AND NOT ONE VARIABLE
        ------------------------------------------
        A GitHub fine-grained token is scoped to exactly one account at
        creation time, in the "Resource owner" dropdown, and that cannot be
        changed afterwards. Today every GitHub repo is under `vomeadmin`
        (VomeApp moved there from a personal account on 2026-10-18), but a
        second account would need a second token.

        Using the wrong one does not look like an auth problem. GitHub
        answers a request for a repo outside the token's owner with 404, not
        403, so it reads as "that file does not exist" and sends you looking
        for a renamed module.

        Resolution order: the account's named token (GITHUB_TOKEN_ALIASES),
        then the owner-specific variable, then the shared
        VOMEOS_GITHUB_TOKEN, which keeps a single-owner setup working with
        no changes.
        """
        env_name = self.token_env_name(owner or self.owner)
        if env_name:
            slug = env_name[len("VOMEOS_GITHUB_TOKEN_"):]
            for alias in GITHUB_TOKEN_ALIASES.get(slug, ()):
                if os.environ.get(alias):
                    return os.environ[alias]
            scoped = os.environ.get(env_name, "")
            if scoped:
                return scoped
        return os.environ.get("VOMEOS_GITHUB_TOKEN", "")

    def configured(self, owner: str = "") -> bool:
        """With an owner: can we reach that account. Without: any at all.

        The two readings matter because `Connector._call` asks the no-owner
        question as a guard before every request, and it must not answer "no"
        just because the default owner is unset. The per-call headers carry
        the owner-specific token.
        """
        if owner:
            return bool(self.token_for(owner))
        if os.environ.get("VOMEOS_GITHUB_TOKEN"):
            return True
        return bool(github_owners_configured())

    def headers(self, owner: str = "") -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token_for(owner)}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def search_code(
        self, query: str, repo: str = "", owner: str = "", ref: str = ""
    ) -> IntegrationResult:
        """GitHub code search, falling back to a grep of the live code.

        GitHub's index covers the default branch only, and for vome-react it
        never completes: every query returns no items with
        `incomplete_results`. In that case the repository is downloaded at
        `ref` and searched directly (see code_mirror.py). If that also
        fails, the original incomplete result is returned with
        `mirror_error`, which callers report as "not searched".
        """
        if repo and not _repo_permitted(repo, self.allowlist):
            return self._denied("search_code", repo)
        who = owner or self.owner
        scope = f"repo:{who}/{repo}" if repo else f"user:{who}"
        result = self._call(
            "search_code",
            "GET",
            "/search/code",
            params={"q": f"{query} {scope}", "per_page": 20},
            # text-match adds the matching fragments, not just the paths.
            headers={**self.headers(who),
                     "Accept": "application/vnd.github.text-match+json"},
        )
        data = result.data if isinstance(result.data, dict) else {}
        if not (repo and result.ok and data.get("incomplete_results")
                and not data.get("items")):
            return result
        from vomeos.integrations import code_mirror

        mirrored = code_mirror.search(who, repo, query, self.headers(who),
                                      ref=ref)
        if mirrored.ok:
            return mirrored
        data["mirror_error"] = mirrored.error
        return result

    def read_file(
        self, repo: str, path: str, ref: str = "", owner: str = "",
        max_bytes: int = 0,
    ) -> IntegrationResult:
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("read_file", repo)
        who = owner or self.owner
        params = {"ref": ref} if ref else None
        result = self._call(
            "read_file",
            "GET",
            f"/repos/{who}/{repo}/contents/{path}",
            params=params,
            headers=self.headers(who),
        )
        return _truncate(result, max_bytes)

    def recent_commits(
        self,
        repo: str,
        path: str = "",
        limit: int = 10,
        ref: str = "",
        owner: str = "",
    ) -> IntegrationResult:
        """What changed here lately, on a named branch.

        `ref` matters as much as `path`. The question behind regression triage
        is "what shipped just before this started", and the answer differs per
        branch: an error from the dev deployment was caused by something on
        `development`, not by whatever last landed on the default branch.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("recent_commits", repo)
        params = {"per_page": min(int(limit), 50)}
        if path:
            params["path"] = path
        if ref:
            params["sha"] = ref
        who = owner or self.owner
        return self._call(
            "recent_commits",
            "GET",
            f"/repos/{who}/{repo}/commits",
            params=params,
            headers=self.headers(who),
        )

    def compare(
        self, repo: str, base: str, head: str, owner: str = ""
    ) -> IntegrationResult:
        """How `head` relates to `base`.

        `data["status"]` is "ahead" or "identical" exactly when `base` is an
        ancestor of `head`, which makes `compare(repo, sha, "master")` the
        "is this commit on master" check. "diverged" and "behind" mean it is
        not. Only the status is wanted, so the commit and file lists that come
        back with it are dropped rather than carried around.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("compare", repo)
        who = owner or self.owner
        result = self._call(
            "compare",
            "GET",
            f"/repos/{who}/{repo}/compare/{base}...{head}",
            params={"per_page": 1},
            headers=self.headers(who),
        )
        if result.ok and isinstance(result.data, dict):
            result.data = {
                k: result.data.get(k)
                for k in ("status", "ahead_by", "behind_by", "total_commits")
            }
        return result

    def pull_request(
        self, repo: str, number: str, owner: str = ""
    ) -> IntegrationResult:
        """One pull request: `merged` and `merge_commit_sha` once merged."""
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("pull_request", repo)
        who = owner or self.owner
        return self._call(
            "pull_request",
            "GET",
            f"/repos/{who}/{repo}/pulls/{number}",
            headers=self.headers(who),
        )

    def workflow_runs(
        self,
        repo: str,
        workflow: str,
        branch: str = "",
        limit: int = 10,
        owner: str = "",
    ) -> IntegrationResult:
        """Recent runs of one workflow, newest first.

        `workflow` is the file name (`cicd-prod.yml`) or numeric id. Needs
        Actions: read on a fine-grained token.
        """
        if not _repo_permitted(repo, self.allowlist):
            return self._denied("workflow_runs", repo)
        who = owner or self.owner
        params: dict = {"per_page": min(int(limit), 50)}
        if branch:
            params["branch"] = branch
        return self._call(
            "workflow_runs",
            "GET",
            f"/repos/{who}/{repo}/actions/workflows/{workflow}/runs",
            params=params,
            headers=self.headers(who),
        )

    def _denied(self, operation: str, repo: str) -> IntegrationResult:
        return IntegrationResult(
            self.system, operation, False,
            error=_denial(repo, self.allowlist),
        )


def _truncate(
    result: IntegrationResult, max_bytes: int = 0
) -> IntegrationResult:
    """Cap a file response so one large module cannot swamp the context.

    A caller that windows around a line passes a larger `max_bytes`: the
    default cap cut `birthday_tasks.py` near line 1300, so every frame below
    it came back as an empty window.
    """
    cap = max_bytes or MAX_FILE_BYTES
    if not result.ok or not isinstance(result.data, str):
        return result
    if len(result.data) <= cap:
        return result
    result.data = (
        result.data[:cap]
        + f"\n\n... truncated at {cap} bytes ..."
    )
    return result


def github_owners_configured() -> tuple[str, ...]:
    """Owners we hold a token for, found by scanning the environment.

    Discovered rather than listed, so adding a third account is a Railway
    variable and not a code change.
    """
    github = GitHub()
    owners = set()
    for name in os.environ:
        if name.startswith("VOMEOS_GITHUB_TOKEN_") and os.environ[name]:
            owners.add(name[len("VOMEOS_GITHUB_TOKEN_"):].lower())
    for slug, aliases in GITHUB_TOKEN_ALIASES.items():
        if any(os.environ.get(alias) for alias in aliases):
            owners.add(slug.lower())
    if os.environ.get("VOMEOS_GITHUB_TOKEN") and github.owner:
        owners.add(github.owner.lower())
    return tuple(sorted(owners))


def describe() -> dict[str, object]:
    """Which code sources are reachable, for the CLI and the health check."""
    bitbucket, github = Bitbucket(), GitHub()
    return {
        "bitbucket": "configured" if bitbucket.configured() else "not set",
        "github": "configured" if github.configured() else "not set",
        "github_owners": list(github_owners_configured()) or ["(none)"],
        "allowlist": (
            list(allowed_repos()) or ["(no allowlist: all reachable)"]
        ),
        "operations": [
            "search_code", "read_file", "recent_commits",
            "compare / merge_base", "workflow_runs / pipelines",
            "pull_request",
        ],
        "writes": "none available",
    }
