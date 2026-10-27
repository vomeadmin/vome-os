"""
security_audit.py

A recurring security audit of Vome's own code. Each week one area of the
product is read in depth by the security auditor agent; anything it finds
is posted to #bug-reports as its own report, where the bug report analyst
checks it against the code again and it is fixed, greenlit or routed like
any other report.

WHY THIS EXISTS
---------------
Sam, 2026-10-26: a pilot run of Cloudflare's security-audit method found
that the mobile Google sign-in trusted the email in the request body (any
account could be taken over, live for 19 months), plus around thirty
cross-organization and admin-role gaps. The dependency watch only covers
known flaws in packages we use; nothing read our own code for this. Sam
wants the analysis to run "once a week or once a month" on its own.

FLOW
----
    Monday 06:00 (engineering.security_audit)
      1. pick the area audited longest ago (never-audited areas first)
      2. engineering.security_auditor reads that area on the production
         branch and returns up to six findings, each with the attacker, the
         route, the missing check (file and line) and the smallest fix.
         It is told everything already reported, so nothing repeats.
      3. each new finding (by fingerprint) is posted to #bug-reports as
         "[Security audit] <title>" and handed to bug_reports, exactly as a
         person's post would be: the analyst re-checks it, then a pull
         request, a greenlight question for Sam, or a task for OnlyG or
         Sanjay. Every permissions fix waits for Sam's yes there.
      4. the run is recorded; a clean run posts nothing.

Eight areas cover core in about eight weeks, then the services and the web
app; the rotation then starts again, so each area is read roughly every
three months, and changed code is read again on its next turn.

WHAT IT NEVER DOES
------------------
Read-only code access, production branch only. It never runs code, never
calls a deployed Vome endpoint and never writes to a repo; fixes only come
from the bug report pipeline, which has its own guards.

SWITCHES
--------
    SECURITY_AUDIT_ENABLED   default on. "false" stops the weekly run.
    SECURITY_AUDIT_CRON      default "0 6 * * mon".
    SLACK_CHANNEL_BUG_REPORTS  where findings go (bug_reports.CHANNEL).

By hand: `py security_audit.py --list`, `py security_audit.py --area
core-auth --dry-run` (prints the findings, posts nothing).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from vomeos import store

AGENT = "engineering.security_auditor"
MAX_POSTS = 6

TABLE = "vomeos_security_audit"
RUNS = "vomeos_security_audit_runs"
store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        fingerprint   VARCHAR PRIMARY KEY,
        area          VARCHAR NOT NULL,
        title         TEXT NOT NULL,
        severity      VARCHAR,
        report_ts     VARCHAR,
        created_at    TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
)
store.register_table(
    RUNS,
    f"""
    CREATE TABLE IF NOT EXISTS {RUNS} (
        area          VARCHAR PRIMARY KEY,
        last_run_at   TIMESTAMP NOT NULL DEFAULT NOW(),
        result        TEXT
    )
    """,
)


@dataclass(frozen=True)
class Area:
    key: str
    title: str
    focus: str
    starting_paths: tuple[str, ...]


AREAS: tuple[Area, ...] = (
    Area("core-auth", "core sign-in and sessions",
         "Identity proof in every sign-in path (password, 2FA, social, SSO, "
         "kiosk, one-time links), token lifetime and revocation, websocket "
         "authentication, service-to-service authentication",
         ("core:user_app/api.py", "core:user_app/serializers.py",
          "core:user_app/kiosk_auth.py", "core:user_app/token_revocation.py",
          "core:vome_project/middleware.py", "core:authentication_app")),
    Area("core-admin", "core admin roles and admin accounts",
         "Admin hierarchy (account holder, super admin, admin, custom role) "
         "and custom-role permissions enforced on the server",
         ("core:admin_permissions_app", "core:organization_app/api.py",
          "core:organization_app/serializers.py",
          "core:organization_app/admin_replacement_api.py")),
    Area("core-opportunity",
         "core opportunities, roles and shifts (admin side)",
         "Every admin write and export bound to the caller's institution",
         ("core:opportunity_app/api.py", "core:opportunity_app/institution",
          "core:opportunity_app/api_version",
          "core:opportunity_app/serializers_related_field.py")),
    Area("core-organization", "core organizations and the volunteer database",
         "Volunteer profiles, merges, messaging, imports, deletions and "
         "integrations bound to the caller's institution",
         ("core:organization_app", "core:volunteer_database_app",
          "core:volunteer_app")),
    Area("core-forms", "core forms and the volunteer side",
         "Form submissions, files and automations bound to the institution "
         "and owner; volunteer enrollment, reservation and check-in bound to "
         "the volunteer",
         ("core:form_app", "core:opportunity_app/user",
          "core:opportunity_app/shift_series")),
    Area("core-public", "core public and token-holder pages",
         "Anonymous routes and invite, signup, share and guest tokens: "
         "guessability, expiry, single use, and what they disclose",
         ("core:mixins.py", "core:organization_app/api.py",
          "core:opportunity_app/api.py", "core:form_app/generic_volunteer")),
    # Split after the first dry run (2026-10-26): as one area it ran out of
    # budget in courses and resources and never reached analytics.
    Area("core-courses", "core courses and resources",
         "Who can see, enroll in and edit courses and resources: membership, "
         "admin-only content, and every id bound to the institution",
         ("core:course_app", "core:resource_app", "core:recognition_app")),
    Area("core-sequences", "core sequences, analytics and notifications",
         "Institution scope on every read and write, exports and reports",
         ("core:sequence_app", "core:analytics_app",
          "core:notifications_app", "core:insights_app")),
    Area("database", "volunteer database service",
         "How the service authenticates callers and scopes every query to "
         "the caller's institution",
         ("database:",)),
    Area("chats", "chat service",
         "Who can read or post in a channel, attachments, service-to-service "
         "authentication",
         ("chats:",)),
    Area("integrations", "integrations service",
         "SSO (SAML) assertion checks, Salesforce and calendar callbacks, "
         "webhooks and stored credentials",
         ("integrations:",)),
    Area("web", "React web app",
         "Rendering of user content (dangerouslySetInnerHTML, rich text), "
         "token storage, open redirects, links built from user data",
         ("web:src",)),
)
AREA_BY_KEY = {a.key: a for a in AREAS}

# Reported by the 2026-10-26 pilot (#bug-reports, "[Security audit n/31]",
# and the Google mobile sign-in fix). The auditor is told these so the
# first pass over each area does not repeat them.
PILOT_REPORTED: dict[str, tuple[str, ...]] = {
    "core-auth": (
        "Google mobile sign-in trusts the email in the request body "
        "(user_app/api.py GoogleAuthApi is_mobile)",
        "Microsoft login accepts any Microsoft token and links by mail",
        "2FA flow: opt-in 2FA depends on a client flag, verify and resend "
        "unthrottled, TOTP replay",
        "Kiosk login skips 2FA and SSO: deliberate, never report",
        "Kiosk tokens reach more than check-in (prefix allowlist, "
        "service-ticket, websockets)",
        "SSO one-time login code has no ttl and is reusable",
        "Sessions survive password reset, refresh renews forever, "
        "websockets ignore revocation and Origin",
        "Service secret == comparisons, Apple iss, ForgotPassword names, "
        "CheckEmailExists unthrottled",
    ),
    "core-admin": (
        "Users & Access role permission enforced only in the frontend",
        "Any admin can promote themselves to Account Holder or remove the "
        "account holder",
        "Deleting a role turns its signup links into Super Admin links; "
        "max_uses race",
        "UpdateProfileFields lets a custom role widen its own visible fields",
        "AdminPermissionsAccessAPIView reads another org's role permissions",
        "Admin signup link update and delete are not scoped to the org",
        "get_admin_role_permissions fails open for a custom role with no "
        "role record",
    ),
    "core-opportunity": (
        "Shift location and shift template update/delete across orgs",
        "checkout-all: a volunteer can check out any shift",
        "SendOpportunityRoleShiftsNotificationAPI: anyone triggers another "
        "org's email blast",
        "Chat room enable/disable/create with an unscoped role or shift",
        "Role move into another org's opportunity (new_opportunity_id, "
        "opportunity_id)",
        "AdminUserV2PrimaryKeyRelatedField and custom tags accept any "
        "volunteer on the platform",
        "Off-Vome hours views: nested permission list and no tenant check",
        "Bulk duplicate copies another org's role or publishes roles in "
        "another org's opportunity (opportunity_app/api.py:2259)",
    ),
    "core-courses": (
        "Any volunteer can see, enroll in and take another org's courses, "
        "including admin-only (course_app/visibility.py:16)",
        "Course coordinator and watcher ids accept any user and expose "
        "their name and email",
        "Course site links, resource folder and course folder accept "
        "another org's ids",
    ),
    "core-organization": (
        "Admin time off by any email cancels shifts in other orgs",
        "merge-vome-profiles overwrites any user's identity; "
        "volunteer-db/volunteer/add accepts any volunteer",
        "send-message recipients are any volunteer on the platform",
        "Bulk offline-user deletion deletes any inactive user platform-wide",
        "Double the Donation endpoints have no org check; company info by "
        "user id; hardcoded DTD keys",
        "Kiosk websocket accepts anonymous subscribers to any kiosk",
        "dump.rdb Redis dump committed to the repo",
    ),
    "core-forms": (
        "DeleteFormResponseAPIView deletes another org's submissions",
        "submitted_for_user accepts any user and forges profile consent",
        "Series self-reservation skips role approval",
        "Add required documents and screening answers to another "
        "volunteer's enrollment",
        "Form builder links another org's questions and documents; form "
        "layout settings unscoped form",
        "Guest submission with a member's email runs automations on the "
        "real account; guest_claim reveals membership",
    ),
    "core-public": (
        "Guest submission with a member's email runs automations on the "
        "real account",
        "Admin invite, guest invite and guest identity tokens never expire",
        "Sterling webhook stores the Authorization header",
    ),
}


def enabled() -> bool:
    value = os.environ.get("SECURITY_AUDIT_ENABLED", "")
    return value.strip().lower() not in ("false", "0", "no", "off")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _known(area: str) -> list[dict]:
    if not store.ensure_table(TABLE):
        return []
    return store.query(
        f"SELECT fingerprint, title FROM {TABLE} WHERE area = :area",
        {"area": area}) or []


def _is_known(fingerprint: str) -> bool:
    rows = store.query(
        f"SELECT 1 FROM {TABLE} WHERE fingerprint = :fp", {"fp": fingerprint})
    return bool(rows)


def _remember(finding: dict, area: str, report_ts: str) -> None:
    store.execute(
        f"""INSERT INTO {TABLE} (fingerprint, area, title, severity, report_ts)
            VALUES (:fp, :area, :title, :severity, :ts)
            ON CONFLICT (fingerprint) DO NOTHING""",
        {"fp": finding["fingerprint"], "area": area,
         "title": finding.get("title") or "",
         "severity": finding.get("severity_guess") or "", "ts": report_ts})


def _record_run(area: str, result: dict) -> None:
    if not store.ensure_table(RUNS):
        return
    store.execute(
        f"""INSERT INTO {RUNS} (area, last_run_at, result)
            VALUES (:area, NOW(), :result)
            ON CONFLICT (area) DO UPDATE
            SET last_run_at = NOW(), result = EXCLUDED.result""",
        {"area": area, "result": json.dumps(result, default=str)[:4000]})


def pick_area() -> Area:
    """The area audited longest ago; never-audited areas first, in order."""
    last = {}
    if store.ensure_table(RUNS):
        for row in store.query(f"SELECT area, last_run_at FROM {RUNS}") or []:
            last[row["area"]] = row["last_run_at"]
    never = [a for a in AREAS if a.key not in last]
    if never:
        return never[0]
    return min(AREAS, key=lambda a: last[a.key])


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def already_reported(area: Area) -> str:
    lines = [f"- {t}" for t in PILOT_REPORTED.get(area.key, ())]
    lines += [f"- {r['title']} ({r['fingerprint']})" for r in _known(area.key)]
    return "\n".join(lines) or "(nothing reported yet in this area)"


def new_findings(card: dict) -> list[dict]:
    """The auditor's findings worth posting, strongest first, never one
    already posted, at most MAX_POSTS."""
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    out, seen = [], set()
    for finding in card.get("findings") or []:
        fingerprint = str(finding.get("fingerprint") or "").strip()
        if not fingerprint or not finding.get("title") or fingerprint in seen:
            continue
        seen.add(fingerprint)
        if _is_known(fingerprint):
            continue
        out.append(finding)
    out.sort(key=lambda f: rank.get(str(f.get("severity_guess")), 4))
    return out[:MAX_POSTS]


def report_text(finding: dict, area: Area) -> str:
    """One #bug-reports post, written like the pilot's reports so the
    analyst reads the same shape."""
    evidence = "\n".join(f"- `{e}`" for e in finding.get("evidence") or [])
    parts = [
        f"[Security audit] {finding['title']}",
        "",
        f"*Who:* {finding.get('attacker') or 'not stated'}. "
        f"*Route:* `{finding.get('route') or 'not stated'}`. "
        f"*Missing check:* `{finding.get('where') or 'not stated'}`.",
        "",
        str(finding.get("what_happens") or ""),
    ]
    if evidence:
        parts += ["", evidence]
    parts += [
        "",
        f"Expected: {finding.get('expected') or 'not stated'}",
        f"Suggested fix: {finding.get('fix') or 'not stated'}",
        "",
        f"Found by the weekly security audit ({area.title}; "
        f"{finding.get('severity_guess') or 'unrated'}, "
        f"{finding.get('confidence') or 'unrated'} confidence; source "
        "trace, not reproduced).",
    ]
    return "\n".join(parts)


def _post(text: str) -> str:
    """Post to #bug-reports as the bot; the message ts, or ""."""
    from slack_sdk import WebClient

    from bug_reports import CHANNEL

    try:
        client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        return client.chat_postMessage(
            channel=CHANNEL, text=text, unfurl_links=False,
            unfurl_media=False).get("ts") or ""
    except Exception as exc:
        print(f"[SECURITY AUDIT] Slack post failed: {exc}")
        return ""


def _file(finding: dict, area: Area) -> str:
    """Post one finding and start the bug report pipeline on it. The
    channel listener ignores bot posts, so this hands it over directly."""
    import bug_reports

    text = report_text(finding, area)
    ts = _post(text)
    if ts:
        bug_reports.handle_message({"ts": ts, "text": text})
    return ts


def _log_failure(area: Area, why: str) -> None:
    try:
        import workflow_log

        summary = f"The weekly security audit of {area.title} did not finish"
        workflow_log.log_issue("security audit", summary, summary,
                               f"Area {area.key}: {why}")
    except Exception as exc:
        print(f"[SECURITY AUDIT] could not log the failure: {exc}")


def audit_area(key: str, dry_run: bool = False) -> dict:
    area = AREA_BY_KEY[key]
    from vomeos import run_agent

    context = {
        "area": area.title,
        "focus": area.focus,
        "starting_paths": ", ".join(area.starting_paths),
        "already_reported": already_reported(area),
    }
    result = run_agent(AGENT, context=context,
                       subject_type="security_audit", subject_id=area.key)
    if not result.ok:
        why = result.why()
        if not dry_run:
            _log_failure(area, why)
            _record_run(area.key, {"status": "error", "why": why})
        return {"status": "error", "area": area.key, "why": why}

    card = dict(result.data or {})
    findings = new_findings(card)
    posted = []
    for finding in findings:
        if dry_run:
            print(report_text(finding, area), "\n" + "-" * 60)
            continue
        ts = _file(finding, area)
        if ts:
            _remember(finding, area.key, ts)
            posted.append(finding["fingerprint"])
    out = {"status": "ok", "area": area.key, "verdict": card.get("verdict"),
           "found": len(card.get("findings") or []), "new": len(findings),
           "posted": posted, "summary": card.get("summary") or ""}
    if not dry_run:
        _record_run(area.key, out)
    return out


def run_weekly_audit() -> dict:
    """Every Monday. Silent when the area is clean."""
    if not enabled():
        return {"status": "skipped", "reason": "SECURITY_AUDIT_ENABLED is off"}
    if not store.ensure_table(TABLE):
        return {"status": "skipped", "reason": "no database"}
    return audit_area(pick_area().key)


def _main(argv: list[str]) -> int:
    if "--list" in argv:
        for area in AREAS:
            print(f"{area.key:18} {area.title}")
        return 0
    if "--area" in argv:
        key = argv[argv.index("--area") + 1]
        print(json.dumps(audit_area(key, dry_run="--dry-run" in argv),
                         indent=2, default=str))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
