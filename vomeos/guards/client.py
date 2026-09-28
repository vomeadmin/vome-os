"""
vomeos/guards/client.py

The manifest-facing wrapper around the outbound check.

`vomeos.guards.outbound` holds the logic and the incident history. This
exposes it under the name a manifest declares, and pulls the three facts it
needs about the recipient out of the run context.
"""

from __future__ import annotations

from vomeos.guards import GuardResult, guard
from vomeos.guards.outbound import validate_client_message


@guard("client_message")
def _client_message(text: str, ctx: dict) -> GuardResult:
    """Output reads as a message to someone outside the company.

    Three inputs come from the run context because they are facts about the
    recipient, not about the agent:

      guard_category     labels the alert a human sees
      contact_name       stops a customer called Sam tripping the
                         internal-names check
      require_signature  False for agents whose caller appends the signature
                         after the guard runs
    """
    result = validate_client_message(
        text,
        category=str(ctx.get("guard_category") or ctx.get("agent") or ""),
        contact_name=str(ctx.get("contact_name") or ""),
        require_signature=bool(ctx.get("require_signature", True)),
    )
    return GuardResult(
        "client_message",
        bool(result.get("ok")),
        "; ".join(result.get("reasons") or []),
        value=result,
    )
