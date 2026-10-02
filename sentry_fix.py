"""
sentry_fix.py

Turn an analyst's diagnosis into a proposed patch, and the patch into a pull
request nobody has to take on trust.

THE LADDER, AND WHERE IT STOPS
------------------------------
1. The analyst says `risk: low` and no migration is needed. Anything else
   stops here, which is most issues.
2. `engineering.fix_author` writes a unified diff, or refuses. Refusing is a
   normal outcome and happens more often than not.
3. `patch_safety` inspects the diff deterministically. Migrations, settings,
   auth, payments, dependencies, CI and test files are refused outright.
4. The diff is APPLIED to the real file in memory and verified line by line.
   If the file has moved since it was read, nothing happens.
5. Only with `SENTRY_AUTO_PR_ENABLED=true` does anything leave this process.

Steps 1 to 4 run whatever the switch says, and the resulting diff goes in the
Slack thread for a human to read. That is the dry run, and it is where this
should live until a month of diffs have been read by somebody.

WHY THE APPLY STEP IS NOT OPTIONAL
----------------------------------
Neither host accepts a patch; both commit whole files. So the diff has to be
applied somewhere, and doing it here means the context lines are verified
against the real file before a branch exists. A diff that no longer fits is
caught as a refusal rather than as a commit that silently deleted the wrong
lines.

It also means a patch that passes every check and still does not apply tells
us something true: the branch moved while we were thinking.
"""

from __future__ import annotations

import os

import sentry_ledger
from vomeos import diff as diff_tools
from vomeos.guards import patch as patch_guard
from vomeos.integrations import code_search, code_write


def auto_pr_enabled() -> bool:
    """The kill switch. Off by default and it should stay off until a month
    of diffs have been read by a person."""
    return os.environ.get("SENTRY_AUTO_PR_ENABLED", "").lower() == "true"


def should_attempt(analysis: dict | None) -> tuple[bool, str]:
    """Whether this diagnosis is a candidate for an automated fix."""
    if not analysis:
        return False, "no analysis"
    if analysis.get("needs_migration"):
        return False, (
            "needs a migration, which is generated on deploy here and never "
            "written by hand"
        )
    risk = str(analysis.get("risk") or "").lower()
    if risk != "low":
        return False, f"risk is {risk or 'unknown'}, not low"
    confidence = str(analysis.get("confidence") or "").lower()
    if confidence == "low":
        return False, "the analyst's own confidence is low"
    return True, "low risk, no migration"


def propose(analysis: dict, context: dict) -> dict:
    """Ask `engineering.fix_author` for a diff, then check it hard.

    Returns a dict the caller can report verbatim. `diff` is only ever
    populated when every check passed.
    """
    ok, reason = should_attempt(analysis)
    if not ok:
        return {"attempted": False, "reason": reason, "diff": ""}

    try:
        from vomeos import run_agent

        result = run_agent(
            "engineering.fix_author",
            context={
                "root_cause": analysis.get("root_cause", ""),
                "proposed_fix": analysis.get("proposed_fix", ""),
                "files": ", ".join(analysis.get("files") or []),
                "source": context.get("source", ""),
                "repo": context.get("repo", ""),
                "branch": context.get("branch", ""),
            },
            subject_type="sentry_issue",
            subject_id=context.get("issue_id", ""),
        )
    except Exception as exc:
        return {"attempted": False, "reason": f"fix author failed: {exc}",
                "diff": ""}

    if not result.ok:
        # `blocked` here usually means patch_safety rejected the diff, which
        # is the guard doing its job and worth reporting as such.
        return {
            "attempted": False,
            "reason": f"fix author {result.status}: {result.why()}",
            "diff": "",
        }

    if not result.get("attempted"):
        return {
            "attempted": False,
            "reason": result.get("reason") or "the fix author declined",
            "diff": "",
        }

    patch = str(result.get("diff") or "")
    safe, detail = patch_guard.inspect(patch)
    if not safe:
        return {"attempted": False, "reason": f"patch refused: {detail}",
                "diff": patch}

    return {
        "attempted": True,
        "reason": detail,
        "diff": patch,
        "rationale": result.get("rationale") or "",
        "risks": result.get("risks") or "",
        "files": diff_tools.files_in(patch),
        "run_id": result.run_id,
    }


def _apply_to_repo(repo: str, ref: str, host: str, owner: str,
                   patch: str) -> tuple[dict, str]:
    """Apply each file's hunks to the real current file. (contents, error).

    Reads the file fresh rather than reusing what the analyst saw, because
    the question that matters is whether the patch fits the branch NOW.
    """
    reader = code_search.GitHub() if host == "github" \
        else code_search.Bitbucket()
    kwargs = {"owner": owner} if host == "github" and owner else {}

    contents: dict[str, str] = {}
    for path, file_diff in diff_tools.split_by_file(patch).items():
        result = reader.read_file(repo, path, ref=ref, **kwargs)
        if not result.ok or not isinstance(result.data, str):
            return {}, f"could not read {path}: {result.error[:120]}"
        try:
            contents[path] = diff_tools.apply(result.data, file_diff)
        except diff_tools.DiffError as exc:
            return {}, f"{path}: {exc}"
    return contents, ""


def open_pull_request(issue: dict, proposal: dict, context: dict) -> dict:
    """Branch, commit, PR. Only when the switch is on and the patch applies.

    Never merges, because no code here can. The pull request targets the
    branch the error came from, so a dev-project fix lands as a proposal
    against `development` and a human merges it or does not.
    """
    if not proposal.get("attempted") or not proposal.get("diff"):
        return {"opened": False, "reason": "no patch to open"}
    if not auto_pr_enabled():
        return {"opened": False,
                "reason": "SENTRY_AUTO_PR_ENABLED is off (dry run)"}

    repo = context.get("repo", "")
    ref = context.get("branch", "")
    host = context.get("host", "bitbucket")
    owner = context.get("owner", "")
    issue_id = str(issue.get("issue_id") or "")

    contents, error = _apply_to_repo(
        repo, ref, host, owner, proposal["diff"]
    )
    if error:
        return {"opened": False, "reason": f"patch does not apply: {error}"}

    writer = code_write.writer_for(host)
    kwargs = {"owner": owner} if host == "github" and owner else {}
    if not writer.configured(**kwargs):
        return {"opened": False, "reason": "no write token configured"}

    branch = code_write.branch_name(
        issue_id, (issue.get("triage_summary") or "")[:40]
    )
    try:
        created = writer.create_branch(repo, branch, ref, **kwargs)
        if not created.ok:
            return {"opened": False,
                    "reason": f"could not branch: {created.error[:140]}"}

        message = f"Fix: {issue.get('triage_summary') or issue_id}"[:100]
        for path, body in contents.items():
            written = writer.commit_file(
                repo, branch, path, body, message, **kwargs
            )
            if not written.ok:
                return {"opened": False, "branch": branch,
                        "reason": f"could not commit {path}: "
                                  f"{written.error[:120]}"}

        opened = writer.open_pull_request(
            repo, branch, ref,
            title=message,
            body=_pr_body(issue, proposal, context),
            **kwargs,
        )
    except code_write.WriteDenied as exc:
        return {"opened": False, "reason": f"refused: {exc}"}

    if not opened.ok:
        return {"opened": False, "branch": branch,
                "reason": f"could not open PR: {opened.error[:140]}"}

    url = _pr_url(opened.data)
    sentry_ledger.update(
        issue_id, status=sentry_ledger.STATUS_PR_OPEN, pr_url=url
    )
    return {"opened": True, "url": url, "branch": branch}


def _pr_url(data) -> str:
    if not isinstance(data, dict):
        return ""
    links = data.get("links")
    if isinstance(links, dict):
        html = links.get("html")
        if isinstance(html, dict):
            return str(html.get("href") or "")
    return str(data.get("html_url") or "")


def _pr_body(issue: dict, proposal: dict, context: dict) -> str:
    """The description a reviewer reads.

    Says what it is, what it is not, and what the author was unsure about.
    A pull request that does not admit it was machine written is a pull
    request that gets less scrutiny than it needs.
    """
    lines = [
        "Proposed automatically from a Sentry issue. **Not reviewed by a "
        "person.** Treat this as a suggestion, not a fix.",
        "",
        f"**Sentry issue** {issue.get('permalink') or issue.get('issue_id')}",
        f"**Severity** {issue.get('severity') or 'unrated'}",
        "",
        "### What broke",
        issue.get("triage_summary") or "",
        "",
        "### Root cause",
        proposal.get("root_cause") or context.get("root_cause", ""),
        "",
        "### Why this fixes it",
        proposal.get("rationale") or "",
        "",
        "### What the author was unsure about",
        proposal.get("risks") or "none identified",
        "",
        "---",
        "No migration is involved: migrations are generated on deploy in "
        "this repository and an agent is refused any path under "
        "`migrations/`.",
    ]
    return "\n".join(line for line in lines if line is not None)


def describe() -> dict:
    return {
        "auto_pr_enabled": auto_pr_enabled(),
        "patch_guard": patch_guard.describe(),
        "write": code_write.describe(),
    }
