"""
vomeos/integrations/code_write.py

The only code in Vome OS that can write to a repository. Three operations,
and merge is not one of them.

WHY THIS IS A SEPARATE MODULE WITH SEPARATE CREDENTIALS
-------------------------------------------------------
`code_search.py` says it plainly: "The access control is not an instruction
in a charter that a model might reason around; it is the absence of any code
that could write." That sentence is the best thing in this package and adding
write methods next to the read ones would have destroyed it.

So the read connector keeps its guarantee, and writing lives here, behind a
different token. Compromising the analysis path does not get you a write,
because the analysis path holds a credential that cannot perform one.

WHAT IT CAN DO
--------------
    create_branch       a new branch off an existing ref
    commit_file         write one file on that branch
    open_pull_request   a PR from that branch into a non-default branch

WHAT IT CANNOT DO, STRUCTURALLY
-------------------------------
Merge. Force push. Delete a branch. Push to a default branch. Change a tag,
a release, a webhook or a CI workflow. None of those have code here, which is
the point: an agent cannot reason its way into a capability that does not
exist.

A pull request is a proposal. `org/handbook.md` permits work that is
"prepared by you, left for a person to approve", and that is exactly what a
PR against a non-default branch is. Nothing here reaches production and
nothing here reaches a customer.

THE SWITCH
----------
`SENTRY_AUTO_PR_ENABLED` defaults to false and is checked by the CALLER, not
here. This module stays a capability; whether to use it is a policy decision
that belongs where the policy is.

ENVIRONMENT
-----------
    VOMEOS_BITBUCKET_WRITE_TOKEN      contents + pullrequest write
    VOMEOS_BITBUCKET_WORKSPACE        shared with the read connector
    VOMEOS_GITHUB_WRITE_TOKEN         shared fallback
    VOMEOS_GITHUB_WRITE_TOKEN_<OWNER> per account, same rule as reading
    VOMEOS_CODE_WRITE_REPOS           repositories that may be written to.
                                      Empty means NONE, which is the opposite
                                      of the read allowlist and deliberate.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult

# Branches that may never be written to or targeted by a pull request, in any
# repository, whatever the configuration says. Belt to go with the braces of
# server-side branch protection.
PROTECTED = tuple(
    re.compile(p, re.I)
    for p in (
        r"^master$", r"^main$", r"^develop(ment)?$", r"^staging$",
        r"^prod(uction)?", r"^release",
    )
)

BRANCH_PREFIX = os.environ.get("VOMEOS_BRANCH_PREFIX", "agent/sentry")

# Deployment-specific names for the same tokens.
#
# The canonical name encodes the GitHub ACCOUNT, because a fine-grained token
# is bound to one account at creation and the owner is how the code knows
# which token can reach which repository. The names below encode the platform
# instead, which is how they were created in Railway, and renaming a live
# variable is a deploy nobody wanted. So they are aliases rather than a
# rename: the canonical name still wins, and these are only consulted when it
# is absent.
#
# If a third GitHub account ever appears, add the canonical name rather than
# another alias. This table is a bridge, not a pattern to follow.
OWNER_TOKEN_ALIASES = {
    "VOMEADMIN": ("vome-os-sentry-write-web",),
    "SAMFAGEN15": ("vome-os-sentry-write-mobile",),
}


def writable_repos() -> tuple[str, ...]:
    """Repositories an agent may write to.

    Empty means none. That is the reverse of `VOMEOS_CODE_REPOS`, where empty
    means everything the token can reach, and the asymmetry is deliberate:
    forgetting to configure a read allowlist costs some extra reading, and
    forgetting a write allowlist would mean every repository was writable.
    """
    raw = os.environ.get("VOMEOS_CODE_WRITE_REPOS", "")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


def is_protected(branch: str) -> bool:
    name = (branch or "").strip()
    return any(pattern.match(name) for pattern in PROTECTED)


def branch_name(issue_id: str, slug: str = "") -> str:
    """A branch name that says where it came from.

    The Sentry issue id is in it so that a branch found six weeks later can
    be traced back to the bug it claims to fix.
    """
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", slug or "").strip("-").lower()[:40]
    issue = re.sub(r"[^a-zA-Z0-9]+", "-", str(issue_id)).strip("-")
    return f"{BRANCH_PREFIX}/{issue}" + (f"-{clean}" if clean else "")


class WriteDenied(RuntimeError):
    """A write was refused before any request was made."""


def _check(repo: str, branch: str = "", target: str = "") -> None:
    if not writable_repos():
        raise WriteDenied(
            "VOMEOS_CODE_WRITE_REPOS is empty, so no repository is writable. "
            "Add one deliberately."
        )
    if repo not in writable_repos():
        raise WriteDenied(
            f"{repo!r} is not in VOMEOS_CODE_WRITE_REPOS"
        )
    if branch and is_protected(branch):
        raise WriteDenied(
            f"refusing to write to protected branch {branch!r}"
        )
    if target and is_protected(target) and not _allow_protected_target():
        raise WriteDenied(
            f"refusing to open a pull request into {target!r}. Set "
            "VOMEOS_PR_ALLOW_PROTECTED_TARGET=true only if a human reviews "
            "every one."
        )


def _allow_protected_target() -> bool:
    """Whether a PR may target a protected branch.

    A pull request INTO `development` is the normal case and is the thing
    this pipeline is for, so it has to be possible. It is off by default
    because the default should be the one that cannot surprise anybody, and
    a review is a human promise rather than a technical control.
    """
    return os.environ.get(
        "VOMEOS_PR_ALLOW_PROTECTED_TARGET", ""
    ).lower() == "true"


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------

@dataclass
class BitbucketWriter(Connector):
    """Write access to Bitbucket Cloud. Branch, commit, pull request."""

    system: str = "bitbucket-write"
    base_url: str = "https://api.bitbucket.org/2.0"
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    @property
    def workspace(self) -> str:
        return os.environ.get("VOMEOS_BITBUCKET_WORKSPACE", "")

    def configured(self) -> bool:
        return bool(
            os.environ.get("VOMEOS_BITBUCKET_WRITE_TOKEN") and self.workspace
        )

    def headers(self) -> dict[str, str]:
        token = os.environ.get("VOMEOS_BITBUCKET_WRITE_TOKEN", "")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def create_branch(
        self, repo: str, name: str, from_ref: str
    ) -> IntegrationResult:
        _check(repo, branch=name)
        return self._call(
            "create_branch", "POST",
            f"/repositories/{self.workspace}/{repo}/refs/branches",
            json_body={"name": name, "target": {"hash": from_ref}},
        )

    def commit_file(
        self, repo: str, branch: str, path: str, content: str, message: str
    ) -> IntegrationResult:
        """Write one file. Bitbucket's src endpoint is form encoded."""
        _check(repo, branch=branch)
        return self._call(
            "commit_file", "POST",
            f"/repositories/{self.workspace}/{repo}/src",
            form={"message": message, "branch": branch, path: content},
        )

    def open_pull_request(
        self, repo: str, branch: str, target: str, title: str, body: str
    ) -> IntegrationResult:
        _check(repo, branch=branch, target=target)
        return self._call(
            "open_pull_request", "POST",
            f"/repositories/{self.workspace}/{repo}/pullrequests",
            json_body={
                "title": title,
                "description": body,
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": target}},
                "close_source_branch": True,
            },
        )


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

@dataclass
class GitHubWriter(Connector):
    """Write access to GitHub. Branch, commit, pull request."""

    system: str = "github-write"
    base_url: str = "https://api.github.com"
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    @property
    def owner(self) -> str:
        return os.environ.get("VOMEOS_GITHUB_OWNER", "")

    def token_for(self, owner: str = "") -> str:
        """Per-account, same rule as reading. A fine-grained token is bound
        to one account at creation and cannot be moved."""
        slug = re.sub(r"[^A-Za-z0-9]", "_", owner or self.owner or "").upper()
        if slug:
            scoped = os.environ.get(f"VOMEOS_GITHUB_WRITE_TOKEN_{slug}", "")
            if scoped:
                return scoped
            for alias in OWNER_TOKEN_ALIASES.get(slug, ()):
                aliased = os.environ.get(alias, "")
                if aliased:
                    return aliased
        return os.environ.get("VOMEOS_GITHUB_WRITE_TOKEN", "")

    def configured(self, owner: str = "") -> bool:
        if owner:
            return bool(self.token_for(owner))
        if os.environ.get("VOMEOS_GITHUB_WRITE_TOKEN"):
            return True
        return any(
            name.startswith("VOMEOS_GITHUB_WRITE_TOKEN_") and value
            for name, value in os.environ.items()
        )

    def headers(self, owner: str = "") -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token_for(owner)}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def ref_sha(self, repo: str, ref: str, owner: str = "") -> str:
        """The commit a branch points at. Needed to branch from it."""
        who = owner or self.owner
        result = self._call(
            "ref_sha", "GET",
            f"/repos/{who}/{repo}/git/ref/heads/{ref}",
            headers=self.headers(who),
        )
        if not result.ok or not isinstance(result.data, dict):
            return ""
        return str((result.data.get("object") or {}).get("sha") or "")

    def file_sha(
        self, repo: str, path: str, branch: str, owner: str = ""
    ) -> str:
        """GitHub requires the blob sha to replace an existing file."""
        who = owner or self.owner
        result = self._call(
            "file_sha", "GET", f"/repos/{who}/{repo}/contents/{path}",
            params={"ref": branch}, headers=self.headers(who),
        )
        if not result.ok or not isinstance(result.data, dict):
            return ""
        return str(result.data.get("sha") or "")

    def create_branch(
        self, repo: str, name: str, from_ref: str, owner: str = ""
    ) -> IntegrationResult:
        _check(repo, branch=name)
        who = owner or self.owner
        sha = from_ref
        if not re.fullmatch(r"[0-9a-f]{40}", from_ref or ""):
            sha = self.ref_sha(repo, from_ref, owner=who)
        if not sha:
            return IntegrationResult(
                self.system, "create_branch", False,
                error=f"could not resolve {from_ref!r} to a commit",
            )
        return self._call(
            "create_branch", "POST", f"/repos/{who}/{repo}/git/refs",
            json_body={"ref": f"refs/heads/{name}", "sha": sha},
            headers=self.headers(who),
        )

    def commit_file(
        self, repo: str, branch: str, path: str, content: str, message: str,
        owner: str = "",
    ) -> IntegrationResult:
        _check(repo, branch=branch)
        who = owner or self.owner
        body = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        existing = self.file_sha(repo, path, branch, owner=who)
        if existing:
            body["sha"] = existing
        return self._call(
            "commit_file", "PUT", f"/repos/{who}/{repo}/contents/{path}",
            json_body=body, headers=self.headers(who),
        )

    def open_pull_request(
        self, repo: str, branch: str, target: str, title: str, body: str,
        owner: str = "",
    ) -> IntegrationResult:
        _check(repo, branch=branch, target=target)
        who = owner or self.owner
        return self._call(
            "open_pull_request", "POST", f"/repos/{who}/{repo}/pulls",
            json_body={
                "title": title, "body": body,
                "head": branch, "base": target,
            },
            headers=self.headers(who),
        )


def writer_for(host: str):
    return GitHubWriter() if host == "github" else BitbucketWriter()


def describe() -> dict[str, object]:
    """What is reachable, for the health check."""
    bitbucket, github = BitbucketWriter(), GitHubWriter()
    return {
        "bitbucket_write": (
            "configured" if bitbucket.configured() else "not set"
        ),
        "github_write": "configured" if github.configured() else "not set",
        "writable_repos": list(writable_repos()) or ["(none: writes refused)"],
        "branch_prefix": BRANCH_PREFIX,
        "operations": ["create_branch", "commit_file", "open_pull_request"],
        "merge": "no code exists to do it",
        "force_push": "no code exists to do it",
        "delete_branch": "no code exists to do it",
    }
