"""
clickup_webhook_monitor.py

Watch the ClickUp status webhook and get it delivering again before anyone
notices it stopped.

WHY THIS EXISTS: ClickUp increments fail_count on every failed delivery and
suspends the webhook once that counter reaches 100. A suspended webhook sends
NOTHING: not a failed request, not a timeout, zero traffic. So the ON PROD,
user education, needs client info and escalated triggers all stop firing and
the only trace anywhere is a health field on the ClickUp API. The server logs
stay clean, Railway looks healthy, and the board looks normal. The outage is
invisible from every surface a human actually looks at.

That is exactly how it went unnoticed on 2026-09-29: the webhook suspended at
14:01 UTC and four ON PROD tasks were silently dropped before anyone asked why
Railway was quiet.

scripts/reactivate_clickup_webhook.py is the manual recovery path. This module
is the automatic one, and it does the thing the script cannot: it watches
fail_count BETWEEN outages. The counter is not documented as resetting on
success, so a handler that occasionally times out is on a slow countdown,
dropping the odd event for months and then falling off a cliff. Catching
fail_count at 10 turns a silent cliff into an early warning.

Slack is only touched when something is actually wrong, so a healthy webhook
produces no noise and a recovered one goes quiet on the next pass.
"""

import os

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = os.environ.get("CLICKUP_TEAM_ID", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

ENDPOINT = (
    "https://vome-support-agent-production.up.railway.app"
    "/webhook/clickup-status"
)
SPACE_ID = 90114113004

# ClickUp suspends at 100. Ten consecutive failures is already a broken
# handler and about a dozen dropped client emails, so warn there rather than
# waiting for the cliff.
WARN_FAIL_COUNT = int(os.environ.get("CLICKUP_WEBHOOK_WARN_FAILS", "10"))

ALERT_CHANNEL = (
    os.environ.get("SLACK_CHANNEL_SUPPORT_ALERTS")
    or os.environ.get("SLACK_CHANNEL_ENG_ALERTS", "")
)

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


def _call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """One ClickUp API call. Raises so the caller can report the failure."""
    resp = httpx.request(
        method,
        CLICKUP_BASE + path,
        headers={
            "Authorization": CLICKUP_API_TOKEN,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _list_hooks() -> list[dict]:
    return _call(f"/team/{CLICKUP_TEAM_ID}/webhook").get("webhooks", [])


def _reactivate(hook: dict) -> None:
    """Flip a suspended webhook back to active. Idempotent."""
    _call(
        f"/webhook/{hook['id']}",
        method="PUT",
        body={
            "endpoint": hook.get("endpoint") or ENDPOINT,
            "events": hook.get("events") or ["taskStatusUpdated"],
            "status": "active",
        },
    )


def _tasks_in_on_prod() -> list[dict]:
    """Tasks sitting in ON PROD right now.

    After a suspension these are the candidates that may never have fired.
    Naming them in the alert is the difference between "something broke" and
    "these four clients are still waiting".
    """
    try:
        data = _call(
            f"/team/{CLICKUP_TEAM_ID}/task"
            f"?space_ids[]={SPACE_ID}&statuses[]=on%20prod&subtasks=true"
        )
    except Exception as e:
        print(f"[CU WEBHOOK] Could not list ON PROD tasks: {e}")
        return []
    return data.get("tasks", [])


def _alert(text: str) -> None:
    if not ALERT_CHANNEL:
        print("[CU WEBHOOK] No alert channel configured, logging only")
        print(text)
        return
    try:
        _slack.chat_postMessage(channel=ALERT_CHANNEL, text=text)
    except SlackApiError as e:
        print(f"[CU WEBHOOK] Slack alert failed: {e.response['error']}")


def check_clickup_webhook_health(auto_reactivate: bool = True) -> dict:
    """Check the ClickUp webhook, reactivate it if suspended, alert on trouble.

    Returns a summary dict. Safe to run as often as you like: reactivating an
    active webhook is a no-op and Slack is only touched on a real problem.
    """
    if not CLICKUP_API_TOKEN or not CLICKUP_TEAM_ID:
        print("[CU WEBHOOK] CLICKUP_API_TOKEN or CLICKUP_TEAM_ID not set")
        return {"status": "unconfigured"}

    try:
        hooks = _list_hooks()
    except Exception as e:
        # Cannot reach ClickUp. Say so rather than reporting a healthy
        # webhook, which is the one lie that would make this worse than
        # having no monitor at all.
        print(f"[CU WEBHOOK] Could not reach the ClickUp API: {e}")
        _alert(
            ":warning: *ClickUp webhook check failed* — could not reach the "
            f"ClickUp API: `{e}`. Webhook health is unknown right now."
        )
        return {"status": "unreachable", "error": str(e)}

    ours = [h for h in hooks if h.get("endpoint") == ENDPOINT]

    if not ours:
        _alert(
            ":rotating_light: *No ClickUp status webhook is registered.* "
            "The subscription is gone, not suspended, so ON PROD, user "
            "education, needs client info and escalated all fire nothing.\n"
            "Recover with `py scripts/reactivate_clickup_webhook.py "
            "--recreate`."
        )
        return {"status": "missing", "hooks": len(hooks)}

    suspended = [
        h for h in ours
        if (h.get("health") or {}).get("status") != "active"
    ]
    degraded = [
        h for h in ours
        if (h.get("health") or {}).get("status") == "active"
        and int((h.get("health") or {}).get("fail_count") or 0)
        >= WARN_FAIL_COUNT
    ]

    if not suspended and not degraded:
        print(f"[CU WEBHOOK] Healthy ({len(ours)} active)")
        return {"status": "healthy", "hooks": len(ours)}

    if degraded and not suspended:
        counts = ", ".join(
            str((h.get("health") or {}).get("fail_count")) for h in degraded
        )
        _alert(
            ":warning: *ClickUp webhook is failing deliveries.* "
            f"fail_count is at {counts} of 100. ClickUp suspends the webhook "
            "at 100 and never re-enables it. Every failure in the meantime is "
            "a dropped status event, which means a client email that was "
            "never sent.\nWorth finding out what is timing out before it hits "
            "the cliff."
        )
        return {"status": "degraded", "fail_counts": counts}

    # Suspended. Reactivate first, then report, because the report tells
    # people to go re-fire tasks and that is wasted while delivery is off.
    reactivated = []
    failed = []
    if auto_reactivate:
        for hook in suspended:
            try:
                _reactivate(hook)
                reactivated.append(hook["id"])
                print(f"[CU WEBHOOK] Reactivated {hook['id']}")
            except Exception as e:
                failed.append((hook["id"], str(e)))
                print(f"[CU WEBHOOK] Reactivate failed for {hook['id']}: {e}")

    stuck = _tasks_in_on_prod()
    stuck_lines = "\n".join(
        f"  • <{t.get('url')}|{(t.get('name') or t.get('id'))[:70]}>"
        for t in stuck[:15]
    )

    if reactivated and not failed:
        _alert(
            ":rotating_light: *ClickUp webhook was suspended and has been "
            "reactivated automatically.*\n"
            "While it was suspended ClickUp sent nothing at all, so every ON "
            "PROD, user education, needs client info and escalated status "
            "change in that window was silently dropped.\n"
            "*Reactivating does not replay them.* Move each task below out of "
            "its status and back in, one at a time, to re-fire it:\n"
            + (stuck_lines or "  (no tasks currently sitting in ON PROD)")
        )
        return {
            "status": "reactivated",
            "ids": reactivated,
            "on_prod_tasks": len(stuck),
        }

    _alert(
        ":rotating_light: *ClickUp webhook is suspended and could not be "
        "reactivated automatically.* Nothing is being delivered.\n"
        "Recover by hand with `py scripts/reactivate_clickup_webhook.py`, or "
        "`--recreate` if a plain reactivate will not stick.\n"
        + "\n".join(f"  `{i}`: {e}" for i, e in failed)
    )
    return {"status": "suspended", "failed": failed}


if __name__ == "__main__":
    import json

    print(json.dumps(check_clickup_webhook_health(), indent=2))
