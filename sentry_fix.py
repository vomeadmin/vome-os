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
import sentry_projects
from vomeos import diff as diff_tools
from vomeos.guards import patch as patch_guard
from vomeos.integrations import code_search, code_write


def auto_pr_enabled() -> bool:
    """The kill switch. Off by default and it should stay off until a month
    of diffs have been read by a person."""
    return os.environ.get("SENTRY_AUTO_PR_ENABLED", "").lower() == "true"


def should_attempt(analysis: dict | None,
                   allow_risk: tuple[str, ...] = ("low",)) -> tuple[bool, str]:
    """Whether this diagnosis is a candidate for an automated fix.

    `allow_risk` widens the risk bar for a caller whose policy says so: the
    Sentry ladder builds medium risk fixes too (Sam, 2026-10-20), because a
    person still reviews and merges every PR.
    """
    if not analysis:
        return False, "no analysis"
    if analysis.get("needs_migration"):
        return False, (
            "needs a migration, which is generated on deploy here and never "
            "written by hand"
        )
    risk = str(analysis.get("risk") or "").lower()
    if risk not in allow_risk:
        return False, (f"risk is {risk or 'unknown'}, not "
                       + " or ".join(allow_risk))
    confidence = str(analysis.get("confidence") or "").lower()
    if confidence == "low":
        return False, "the analyst's own confidence is low"
    return True, f"{risk} risk, no migration"


def propose(analysis: dict, context: dict,
            allow_risk: tuple[str, ...] = ("low",)) -> dict:
    """Ask `engineering.fix_author` for a diff, then check it hard.

    Returns a dict the caller can report verbatim. `diff` is only ever
    populated when every check passed.
    """
    ok, reason = should_attempt(analysis, allow_risk)
    if not ok:
        return {"attempted": False, "reason": reason, "diff": ""}

    try:
        from vomeos import run_agent

        fix_context = {
            "root_cause": analysis.get("root_cause", ""),
            "proposed_fix": analysis.get("proposed_fix", ""),
            "files": ", ".join(analysis.get("files") or []),
            "source": context.get("source", ""),
            "repo": context.get("repo", ""),
            "branch": context.get("branch", ""),
        }
        # #bug-reports: Sam read the problem, fix and risk and said yes. The
        # fix author's skill building-a-greenlit-fix reads this section.
        if context.get("greenlight"):
            fix_context["greenlight"] = context["greenlight"]
        # The applier refused this author's last diff for the same fix; the
        # skill building-a-greenlit-fix says how to write it again.
        if context.get("previous_attempt"):
            fix_context["previous_attempt"] = context["previous_attempt"]
        result = run_agent(
            "engineering.fix_author",
            context=fix_context,
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
            # True when the only blocker was source it was not shown. The
            # feature desk holds those for `go N` instead of logging them.
            "needs_context": bool(result.get("needs_context")),
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


# Large enough for any source file we hold (MyVolunteerUserList.js is ~295 KB)
# and under GitHub's 1 MB limit for inline file contents.
APPLY_MAX_BYTES = 1_000_000


def _file_text(result) -> tuple[str | None, str]:
    """The full text of a read_file result, or (None, why not).

    Bitbucket returns the raw file. GitHub's contents API returns JSON with
    the file base64 encoded, which this step used to reject as "not text",
    so no agent fix could ever open on a GitHub repository (#9582,
    2026-10-19). A truncated read is refused outright.
    """
    if not result.ok:
        return None, result.error[:120]
    data = result.data
    if isinstance(data, dict) and data.get("encoding") == "base64":
        import base64

        data = base64.b64decode(data.get("content") or "").decode("utf-8")
    if not isinstance(data, str):
        return None, "the response was not a file"
    if data.endswith(f"... truncated at {APPLY_MAX_BYTES} bytes ..."):
        return None, f"larger than {APPLY_MAX_BYTES} bytes"
    return data, ""


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
        # The whole file, not the 60 KB reading cap: whatever comes back here
        # is what gets committed, so a cut-off file would become a PR that
        # deletes everything past the cut.
        result = reader.read_file(repo, path, ref=ref,
                                  max_bytes=APPLY_MAX_BYTES, **kwargs)
        text, why = _file_text(result)
        if text is None:
            return {}, f"could not read {path}: {why}"
        try:
            contents[path] = diff_tools.apply(text, file_diff)
        except diff_tools.DiffError as exc:
            return {}, f"{path}: {exc}"
    return contents, ""


def open_pull_request(issue: dict, proposal: dict, context: dict) -> dict:
    """Branch, commit, PR. Only when the switch is on and the patch applies.

    Never merges, because no code here can.

    THE FIX DOES NOT GO WHERE THE ERROR CAME FROM
    ---------------------------------------------
    The analyst reads the branch that produced the traceback, because that is
    the code which actually ran. A fix for it belongs on the integration
    branch, never on `master`: a pull request into a production branch is one
    click from production and skips development and staging on the way.

    So the error's ref is used for diagnosis and plays no part below. The
    branch is cut from the target, the patch is applied to the target's copy
    of the file, and the pull request goes back into the target. A repository
    with no integration branch is refused rather than defaulted, because the
    default would be `master`.

    A patch written against `master` may not fit `development`. That is fine
    and is why the context lines are verified: a patch that no longer fits is
    refused rather than applied at an offset.
    """
    if not proposal.get("attempted") or not proposal.get("diff"):
        return {"opened": False, "reason": "no patch to open"}
    if not auto_pr_enabled():
        return {"opened": False,
                "reason": "SENTRY_AUTO_PR_ENABLED is off (dry run)"}

    repo = context.get("repo", "")
    host = context.get("host", "bitbucket")
    owner = context.get("owner", "")
    issue_id = str(issue.get("issue_id") or "")

    ref = sentry_projects.fix_target(repo)
    if not ref:
        return {
            "opened": False,
            "reason": (
                f"no integration branch configured for {repo!r}, so there is "
                "nowhere to open a pull request that is not a production "
                "branch. Set VOMEOS_PR_TARGETS."
            ),
        }

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

        message = _clip(f"Fix: {issue.get('triage_summary') or issue_id}",
                        100)
        for path, body in contents.items():
            written = writer.commit_file(
                repo, branch, path, body, message, **kwargs
            )
            if not written.ok:
                return {"opened": False, "branch": branch,
                        "reason": f"could not commit {path}: "
                                  f"{written.error[:120]}"}

        named = _reviewer_args(writer, host, context.get("reviewer") or {})
        body = _pr_body(issue, proposal, context)
        opened = writer.open_pull_request(
            repo, branch, ref, title=message, body=body, **kwargs, **named,
        )
        if not opened.ok and named:
            # A reviewer Bitbucket will not accept must not cost the PR.
            named = {}
            opened = writer.open_pull_request(
                repo, branch, ref, title=message, body=body, **kwargs,
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
    if "assignee" in named:
        reviewer_added = bool((opened.data or {}).get("assigned"))
    else:
        reviewer_added = bool(named)
    return {"opened": True, "url": url, "branch": branch,
            "reviewer_added": reviewer_added}


def _clip(text: str, limit: int) -> str:
    """Shorten a title at the last whole word, so it never ends "and when h"."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 3]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "..."


def _reviewer_args(writer, host, reviewer: dict) -> dict:
    """How to name the engineer when the PR opens: a GitHub login as
    assignee, or a Bitbucket member uuid as reviewer. {} when there is
    nobody to name or they cannot be found; the PR opens either way and
    the engineer's DM says to add themselves."""
    if not reviewer:
        return {}
    if host == "github":
        login = reviewer.get("github") or ""
        return {"assignee": login} if login else {}
    try:
        uuid = writer.member_uuid(reviewer.get("name") or "",
                                  reviewer.get("bitbucket_name") or "")
    except Exception as exc:
        print(f"[SENTRY FIX] could not look up {reviewer.get('name')}: {exc}")
        return {}
    return {"reviewers": (uuid,)} if uuid else {}


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
    source = issue.get("source_label") or "a Sentry issue"
    origin = (f"**Source** {source}" if issue.get("source_label") else
              f"**Sentry issue** "
              f"{issue.get('permalink') or issue.get('issue_id')}")
    lines = [
        f"Proposed automatically from {source}. **Not reviewed by a "
        "person.** Treat this as a suggestion, not a fix.",
        "",
        origin,
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
