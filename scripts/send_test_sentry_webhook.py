"""
send_test_sentry_webhook.py

Send one correctly signed, synthetic Sentry webhook at the deployed endpoint,
so the whole path can be proved without waiting for a real bug.

WHY THIS EXISTS
---------------
`issue.created` fires only the first time Sentry has ever seen a group. An
existing issue happening again does not fire it. So a freshly armed pipeline
is silent, and silence is indistinguishable from three different failures: the
secret not matching, the integration not pointing at the URL, or genuinely
nothing new having broken.

This removes the ambiguity. It signs a payload with the same client secret the
server holds, so a 200 proves signature verification, the gate and the ledger
all work, end to end, through the real deployment.

THE SECRET NEVER LEAVES YOUR MACHINE
------------------------------------
It is read from the environment and used only to compute an HMAC. It is not
printed and not sent: the signature goes in the header, the secret does not.

Usage:

    # Prove the path. Sends an issue that SHOULD survive the gate.
    SENTRY_WEBHOOK_SECRET=<client secret> \\
      py scripts/send_test_sentry_webhook.py

    # Point at somewhere else, or test a gate rule.
    py scripts/send_test_sentry_webhook.py --url https://host/webhook/sentry
    py scripts/send_test_sentry_webhook.py --case noise
    py scripts/send_test_sentry_webhook.py --case dev-project
    py scripts/send_test_sentry_webhook.py --case performance

What each response means:

    200 {"status": "queued"}   everything works. The issue is in the ledger.
    200 {"status": "gated"}    works, and a gate rule dropped it (expected
                               for --case noise / dev-project / performance).
    403                        the secret here and the secret on the server
                               disagree, OR the server has none set.
    404                        wrong URL.
    500                        look at the Railway logs, not at this script.

The synthetic issue id is prefixed `test-` and is obvious in the ledger. To
remove it afterwards:

    DELETE FROM vomeos_sentry_issues WHERE issue_id LIKE 'test-%';
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = (
    "https://vome-support-agent-production.up.railway.app/webhook/sentry"
)


def _issue(case: str) -> dict:
    """One synthetic issue per case, shaped like a real issue.created body."""
    stamp = int(time.time())

    if case == "noise":
        # Should be gated by the exception_type rule.
        project, title, culprit, etype, level, category = (
            "dev-vome-app",
            "DisallowedHost: Invalid HTTP_HOST header: 'scanner.example'",
            "django/http/request.py",
            "DisallowedHost",
            "error",
            "error",
        )
    elif case == "dev-project":
        # Should be gated by project_dev, unless dev-vome-app is allowlisted.
        project, title, culprit, etype, level, category = (
            "dev-volunteer-database",
            "AttributeError: synthetic test from send_test_sentry_webhook",
            "apps/custom_fields/apis/v1/serializers.py",
            "AttributeError",
            "error",
            "error",
        )
    elif case == "performance":
        # Should be gated by issue_category.
        project, title, culprit, etype, level, category = (
            "dev-vome-app",
            "N+1 Query",
            "/api/form/institution/general-form/list/",
            "",
            "info",
            "db_query",
        )
    else:
        # The default. Should survive every rule and reach the queue.
        project, title, culprit, etype, level, category = (
            "dev-vome-app",
            "AttributeError: synthetic test from send_test_sentry_webhook",
            "opportunity_app/views.py in reserve_shift",
            "AttributeError",
            "error",
            "error",
        )

    return {
        "action": "created",
        "data": {
            "issue": {
                "id": f"test-{case}-{stamp}",
                "title": title,
                "culprit": culprit,
                "level": level,
                "issueCategory": category,
                "count": 1,
                "userCount": 0,
                "permalink": "https://vome-2j.sentry.io/issues/synthetic/",
                "project": {"slug": project},
                "metadata": {"type": etype} if etype else {},
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--case",
        default="survives",
        choices=("survives", "noise", "dev-project", "performance"),
    )
    parser.add_argument(
        "--bad-signature",
        action="store_true",
        help="Send a deliberately wrong signature. Should get 403.",
    )
    args = parser.parse_args()

    secret = os.environ.get("SENTRY_WEBHOOK_SECRET", "")
    if not secret:
        print(
            "SENTRY_WEBHOOK_SECRET is not set.\n\n"
            "Get it from the Custom Integration's page:\n"
            "  https://vome-2j.sentry.io/settings/developer-settings/\n"
            "and run:\n"
            "  SENTRY_WEBHOOK_SECRET=<client secret> "
            "py scripts/send_test_sentry_webhook.py"
        )
        return 2

    body = json.dumps(_issue(args.case)).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if args.bad_signature:
        signature = "0" * 64

    request = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "sentry-hook-resource": "issue",
            "sentry-hook-signature": signature,
        },
    )

    print(f"POST {args.url}")
    print(f"  case       {args.case}")
    print(f"  issue id   {json.loads(body)['data']['issue']['id']}")
    print(f"  signature  {signature[:12]}... ({len(signature)} chars)")
    print()

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode()
            print(f"HTTP {response.status}")
            print(payload)
            status = json.loads(payload).get("status", "")
            if status == "queued":
                print("\nThe path works. The issue is in the ledger.")
            elif status == "gated":
                print("\nThe path works. A gate rule dropped it, as expected.")
            return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        print(f"HTTP {exc.code}")
        print(detail)
        if exc.code == 403:
            print(
                "\nThe signature was rejected. Either this secret and the "
                "one on the server differ, or the server has none set.\n"
                "Check /health: it reports SENTRY_WEBHOOK_SECRET true/false."
            )
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach {args.url}: {exc.reason}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
