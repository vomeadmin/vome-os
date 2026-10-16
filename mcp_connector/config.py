"""
mcp_connector/config.py

Every environment variable the connector reads, in one place.

Read at call time rather than import time, so a test can set a variable and
a Railway change is picked up by the next request without a code path that
remembers the old value.

ENVIRONMENT
-----------
    MCP_PUBLIC_URL            the service's public origin, no trailing slash,
                              e.g. https://vome-os.up.railway.app. The
                              connector is OFF until this is set.
    MCP_GOOGLE_CLIENT_ID      Google OAuth web client. Its authorised
    MCP_GOOGLE_CLIENT_SECRET  redirect URI must be
                              <MCP_PUBLIC_URL>/oauth/google/callback
    MCP_ALLOWED_DOMAINS       comma separated email domains that may sign in.
                              Default: vomevolunteer.co,vomevolunteer.com
    MCP_ALLOWED_EMAILS        extra individual addresses outside those domains
    MCP_TRIAGE_EMAILS         who may prioritise and move feature requests.
                              Default: s.fagen@vomevolunteer.co,
                              s.fagen@vomevolunteer.com
    MCP_CLICKUP_USERS         email=clickup_user_id pairs, for "assign to me"
    MCP_FEATURE_REQUEST_LIST  ClickUp list new requests land in. Default: Raw
                              Intake
    MCP_ACCESS_TOKEN_TTL      seconds, default 8 hours
    MCP_REFRESH_TOKEN_TTL     seconds, default 30 days
    MCP_LESSONS_DOC_ID        ClickUp doc holding the Sprint Lessons Log
    MCP_LESSONS_PAGE_IDS      comma separated page ids inside it
    MCP_CODE_REPOS            repos the code tools may read. Default: all of
                              them in repos.py. Separate from
                              VOMEOS_CODE_REPOS on purpose.
    MCP_LIVE_BRANCHES         repo=branch overrides, see repos.py
    MCP_PRODUCT_SOURCE_TTL    seconds between refreshes of the frontend
                              source, default 6 hours
    CLICKUP_TEAM_ID           the ClickUp workspace, for the docs API
"""

from __future__ import annotations

import os

DEFAULT_DOMAINS = "vomevolunteer.co,vomevolunteer.com"
DEFAULT_TRIAGE = "s.fagen@vomevolunteer.co,s.fagen@vomevolunteer.com"

# The Support Sprint Playbook doc and its Sprint Lessons Log page.
DEFAULT_LESSONS_DOC = "3dztm-3311"
DEFAULT_LESSONS_PAGES = "3dztm-2491"

# Sam's ClickUp user id, from clickup_tasks.CLICKUP_USER_SAM. Repeated here
# as the default mapping rather than imported, so the mapping is one visible
# env var and not a constant buried in another module.
DEFAULT_CLICKUP_USERS = (
    "s.fagen@vomevolunteer.co=3691763,s.fagen@vomevolunteer.com=3691763"
)

GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
MCP_PATH = "/mcp"
SCOPE = "vome:read"


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(v.strip().lower() for v in raw.split(",") if v.strip())


def public_url() -> str:
    return os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")


def resource_url() -> str:
    return f"{public_url()}{MCP_PATH}"


def google_redirect_uri() -> str:
    return f"{public_url()}{GOOGLE_CALLBACK_PATH}"


def google_client_id() -> str:
    return os.environ.get("MCP_GOOGLE_CLIENT_ID", "").strip()


def google_client_secret() -> str:
    return os.environ.get("MCP_GOOGLE_CLIENT_SECRET", "").strip()


def enabled() -> bool:
    """The connector serves nothing until it can authenticate people.

    An MCP endpoint that exposes product source and the backlog with no
    sign-in is worse than no endpoint, so a partial configuration is OFF
    rather than open.
    """
    return bool(public_url() and google_client_id() and google_client_secret())


def allowed_domains() -> tuple[str, ...]:
    return _csv("MCP_ALLOWED_DOMAINS", DEFAULT_DOMAINS)


def allowed_emails() -> tuple[str, ...]:
    return _csv("MCP_ALLOWED_EMAILS")


def email_allowed(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    if email in allowed_emails():
        return True
    return email.rsplit("@", 1)[1] in allowed_domains()


def triage_emails() -> tuple[str, ...]:
    return _csv("MCP_TRIAGE_EMAILS", DEFAULT_TRIAGE)


def can_triage(email: str) -> bool:
    return (email or "").strip().lower() in triage_emails()


def clickup_user_for(email: str) -> int | None:
    for pair in _csv("MCP_CLICKUP_USERS", DEFAULT_CLICKUP_USERS):
        who, _, user_id = pair.partition("=")
        if who.strip() == (email or "").strip().lower() and user_id.isdigit():
            return int(user_id)
    return None


def feature_request_list() -> str:
    # Raw Intake, from clickup_tasks.LIST_RAW_INTAKE.
    return os.environ.get("MCP_FEATURE_REQUEST_LIST", "901113386484").strip()


def access_token_ttl() -> int:
    return int(os.environ.get("MCP_ACCESS_TOKEN_TTL", str(8 * 3600)))


def refresh_token_ttl() -> int:
    return int(os.environ.get("MCP_REFRESH_TOKEN_TTL", str(30 * 86400)))


def lessons_doc_id() -> str:
    return os.environ.get("MCP_LESSONS_DOC_ID", DEFAULT_LESSONS_DOC).strip()


def lessons_page_ids() -> tuple[str, ...]:
    raw = os.environ.get("MCP_LESSONS_PAGE_IDS", DEFAULT_LESSONS_PAGES)
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def clickup_team_id() -> str:
    return os.environ.get("CLICKUP_TEAM_ID", "").strip()


def clickup_token() -> str:
    return os.environ.get("CLICKUP_API_TOKEN", "").strip()


def code_repos() -> tuple[str, ...]:
    """Repositories the connector's code tools may read.

    Its own list, separate from VOMEOS_CODE_REPOS, which governs the
    autonomous Sentry analyst and is deliberately narrow. Default: every
    repository in repos.py.
    """
    from mcp_connector import repos

    raw = os.environ.get("MCP_CODE_REPOS", "")
    chosen = tuple(r.strip() for r in raw.split(",") if r.strip())
    return chosen or repos.slugs()


def product_source_ttl() -> int:
    return int(os.environ.get("MCP_PRODUCT_SOURCE_TTL", str(6 * 3600)))
