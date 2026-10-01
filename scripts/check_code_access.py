"""
check_code_access.py

Prove a Bitbucket or GitHub read token works, before it goes anywhere near
Railway.

WHY THIS EXISTS
---------------
A code token fails in ways that all look the same from inside an agent: a
404 that actually means "wrong account", a 401 that means "this credential
wanted Basic and you sent Bearer", a 403 that means "the scope is missing",
and a genuine 404 that means the file moved. By the time an analyst hits one,
it surfaces as "could not reach the repository" three layers away from the
cause.

So this runs the exact four read operations the analyst will use, against the
real repository, and prints which ones worked.

Usage:

    # Bitbucket, scoped Atlassian API token (Bearer)
    VOMEOS_BITBUCKET_WORKSPACE=vomedjango \\
    VOMEOS_BITBUCKET_TOKEN=<token> \\
      py scripts/check_code_access.py bitbucket

    # Bitbucket, unscoped token or app password (Basic, needs the email)
    VOMEOS_BITBUCKET_WORKSPACE=vomedjango \\
    VOMEOS_BITBUCKET_EMAIL=you@vomevolunteer.co \\
    VOMEOS_BITBUCKET_TOKEN=<token> \\
      py scripts/check_code_access.py bitbucket

    # GitHub
    VOMEOS_GITHUB_OWNER=samfagen15 VOMEOS_GITHUB_TOKEN=<token> \\
      py scripts/check_code_access.py github --repo VomeApp --ref master

Reading what comes back:

    all four OK          the token is right. Put it on Railway.
    401 everywhere       wrong auth shape. On Bitbucket, try setting
                         VOMEOS_BITBUCKET_EMAIL to switch to Basic.
    403 everywhere       the token authenticated but lacks the read scope.
    404 on read_file     usually the wrong branch, or the wrong owner on
                         GitHub, rather than a missing file.
    search_code fails    some Bitbucket token types cannot search across a
                         workspace even when they can read a repo. Not fatal:
                         the analyst degrades to reading named files.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vomeos.integrations import code_search  # noqa: E402

DEFAULTS = {
    "bitbucket": {
        "repo": "vomedjango-restored-core-app",
        "ref": "development",
        "path": "manage.py",
        "query": "OpportunityShift",
    },
    "github": {
        "repo": "VomeApp",
        "ref": "master",
        "path": "package.json",
        "query": "useEffect",
    },
}


def _show(label: str, result) -> bool:
    if result.ok:
        size = len(result.data) if isinstance(result.data, str) else "ok"
        print(f"  [ OK ] {label:16} {result.duration_ms:>5}ms  {size}")
        return True
    code = result.status_code or "-"
    print(f"  [FAIL] {label:16} HTTP {code}  {result.error[:88]}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", choices=("bitbucket", "github"))
    parser.add_argument(
        "--owner",
        help=(
            "GitHub account. A fine-grained token is scoped to one account, "
            "so each owner needs its own token in "
            "VOMEOS_GITHUB_TOKEN_<OWNER>."
        ),
    )
    parser.add_argument("--repo")
    parser.add_argument("--ref")
    parser.add_argument("--path")
    parser.add_argument("--query")
    args = parser.parse_args()

    d = DEFAULTS[args.host]
    repo = args.repo or d["repo"]
    ref = args.ref or d["ref"]
    path = args.path or d["path"]
    query = args.query or d["query"]

    if args.host == "bitbucket":
        client = code_search.Bitbucket()
        where = f"{client.workspace}/{repo}"
        auth = (
            "Basic (VOMEOS_BITBUCKET_EMAIL is set)"
            if os.environ.get("VOMEOS_BITBUCKET_EMAIL")
            else "Bearer"
        )
    else:
        client = code_search.GitHub()
        owner = args.owner or client.owner
        where = f"{owner}/{repo}"
        auth = f"Bearer ({client.token_env_name(owner)})"

    print(f"{args.host}: {where} @ {ref}")
    print(f"  auth   {auth}")
    print(f"  allow  {list(code_search.allowed_repos()) or '(no allowlist)'}")
    print()

    configured = (
        client.configured(args.owner or client.owner)
        if args.host == "github"
        else client.configured()
    )
    if not configured:
        print(
            "  NOT CONFIGURED. Missing token or workspace/owner.\n"
            "  See the usage block at the top of this file."
        )
        return 2

    if args.host == "github":
        kw = {"owner": args.owner or client.owner}
    else:
        kw = {}

    results = [
        _show("read_file", client.read_file(repo, path, ref=ref, **kw)),
        _show(
            "recent_commits",
            client.recent_commits(repo, limit=3, ref=ref, **kw),
        ),
        _show(
            "commits_on_path",
            client.recent_commits(repo, path=path, limit=3, ref=ref, **kw),
        ),
        _show("search_code", client.search_code(query, repo=repo, **kw)),
    ]

    passed = sum(1 for r in results if r)
    print()
    print(f"{passed}/4 operations succeeded.")
    if passed == 4:
        print("Token is good. Safe to add to Railway.")
        return 0
    if passed >= 2:
        print(
            "Partial. The analyst can work with read_file and recent_commits;\n"
            "search_code failing just means it cannot grep, only read."
        )
        return 0
    print("Not usable yet. Read the failure codes above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
