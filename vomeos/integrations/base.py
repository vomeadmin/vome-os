"""
vomeos/integrations/base.py

The connector contract.

Every system the OS reaches (django-core, the chats service, the integrations
service, Zoho, ClickUp, Slack, Stripe) gets a subclass of `Connector`. The
subclass declares its base URL, its auth, and one method per operation it
offers.

What the base class provides, so no subclass reinvents it:

  * one timeout, from `vomeos.config`, for every outbound call;
  * a uniform result type, so a caller never has to know whether a failure was
    a timeout, a 500 or a bad payload;
  * failures that return rather than raise, because an agent workflow that
    dies on a third party being briefly slow is worse than one that reports
    "could not reach django-core" and takes its safe path;
  * a record of which system was called, for the trace.

What it deliberately does NOT provide: a generic `get(url)`. A connector's
methods are its contract. Adding an operation should be a visible change to
this package, reviewed like any other access grant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from vomeos import config


class ConnectorError(RuntimeError):
    """A connector is misconfigured. Raised at construction, not per call."""


@dataclass
class IntegrationResult:
    """The outcome of one call to another system.

    `ok` False never means "the answer was no". It means we did not get an
    answer. Callers must treat the two differently: a customer who is not
    found is data, a django-core that did not respond is an outage.
    """

    system: str
    operation: str
    ok: bool
    data: Any = None
    status_code: int = 0
    error: str = ""
    duration_ms: int = 0
    # Response headers, lowercased. Empty when the call never got a response.
    #
    # Needed because some facts only arrive this way. GitHub reports a
    # fine-grained token's expiry date in
    # `github-authentication-token-expiration` and nowhere else, and a token
    # that silently expires looks exactly like a repository we cannot read.
    headers: dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default


@dataclass
class Connector:
    """Base class for every outbound integration.

    Subclasses set `system` and `base_url`, override `headers()` if they need
    auth, and expose one method per operation.
    """

    system: str = "unnamed"
    base_url: str = ""
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    def configured(self) -> bool:
        """False when this connector has no base URL or no credentials.

        Checked by callers before use. A connector that is not configured is a
        normal state in a partial environment, not an error.
        """
        return bool(self.base_url)

    def headers(self) -> dict[str, str]:
        """Auth and content headers. Overridden per system."""
        return {"Accept": "application/json"}

    # -----------------------------------------------------------------
    # The single outbound call. Subclasses use this; callers do not.
    # -----------------------------------------------------------------

    def _call(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> IntegrationResult:
        """Make the call. `headers` overrides `self.headers()` for one call.

        The override exists because a connector can need more than one
        credential. GitHub fine-grained tokens are scoped to a single account,
        so reaching two owners means two tokens and the right one has to be
        chosen per call rather than per connector.
        """
        import time

        if not self.configured():
            return IntegrationResult(
                self.system, operation, False,
                error=f"{self.system} connector is not configured",
            )

        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        started = time.monotonic()
        try:
            response = httpx.request(
                method.upper(),
                url,
                headers=headers or self.headers(),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except Exception as exc:
            return IntegrationResult(
                self.system, operation, False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        duration = int((time.monotonic() - started) * 1000)
        headers = {k.lower(): v for k, v in response.headers.items()}
        if response.status_code >= 400:
            return IntegrationResult(
                self.system, operation, False,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}: {response.text[:200]}",
                duration_ms=duration,
                headers=headers,
            )

        try:
            data = response.json()
        except ValueError:
            data = response.text

        return IntegrationResult(
            self.system, operation, True,
            data=data,
            status_code=response.status_code,
            duration_ms=duration,
            headers=headers,
        )
