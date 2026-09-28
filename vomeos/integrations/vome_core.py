"""
vomeos/integrations/vome_core.py

The connector to django-core, the Vome product backend.

This is the reference implementation of the rule in this package: the OS needs
facts that live in the product, and it gets them by asking the product's
support API over HTTP. There is no import of django-core anywhere in Vome OS,
and there must never be one.

The endpoints below already exist and are already used this way by the support
app, which is what makes this a formalisation rather than a new integration.
Adding an operation here is a deliberate, reviewable act.

Environment:

    VOME_CORE_API_URL   base URL. DJANGO_PROD_API_URL and DJANGO_API_URL are
                        accepted as fallbacks so an existing deployment keeps
                        working without an env change.
    VOME_CORE_API_KEY   shared secret. SUPPORT_API_KEY accepted as fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult


def _base_url() -> str:
    return (
        os.environ.get("VOME_CORE_API_URL")
        or os.environ.get("DJANGO_PROD_API_URL")
        or os.environ.get("DJANGO_API_URL", "")
    )


def _api_key() -> str:
    return (
        os.environ.get("VOME_CORE_API_KEY")
        or os.environ.get("SUPPORT_API_KEY", "")
    )


@dataclass
class VomeCore(Connector):
    """Read access to the Vome product backend, for support workflows."""

    system: str = "vome-core"
    base_url: str = field(default_factory=_base_url)
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    def configured(self) -> bool:
        return bool(self.base_url) and bool(_api_key())

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Support-Api-Key": _api_key(),
        }

    # -----------------------------------------------------------------
    # Operations. One method per thing the OS is allowed to ask for.
    # -----------------------------------------------------------------

    def auth_check(self, email: str) -> IntegrationResult:
        """Why a given email cannot sign in.

        Read only. Used by support to answer "I am locked out" without asking
        the customer to try things.
        """
        return self._call(
            "auth_check",
            "POST",
            "/api/support/auth-check/",
            json_body={"email": email},
        )
