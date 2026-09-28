"""
outbound_guard.py

Compatibility shim. The implementation moved into Vome OS.

The outbound check was written here after ticket #8945 was emailed a model's
refusal-to-draft commentary on 2026-09-07. It now lives in
`vomeos/guards/outbound.py`, because every division that sends anything
outward needs it and a safety check that each application re-implements is a
safety check that drifts.

This module stays so existing imports and `test_outbound_guard.py` keep
working. It adds nothing. New code should import from
`vomeos.guards.outbound` directly, or better, declare the `client_message`
guard in the agent's manifest and let the runtime enforce it.
"""

from vomeos.guards.outbound import (  # noqa: F401
    guard_failure_notice,
    validate_client_message,
)

__all__ = ["guard_failure_notice", "validate_client_message"]
