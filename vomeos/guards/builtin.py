"""
vomeos/guards/builtin.py

Shape and size guards. Nothing here knows anything about a customer.
"""

from __future__ import annotations

import json

from vomeos.guards import GuardResult, guard
from vomeos.text import strip_code_fence


@guard("json_shape")
def _json_shape(text: str, ctx: dict) -> GuardResult:
    """Output is, or begins with, a JSON object.

    Trailing content after a complete object is tolerated and reported rather
    than rejected. The model does sometimes append a sentence after the JSON
    even at temperature 0, and the decision it already gave is perfectly
    usable. Rejecting it would send a correct verdict to the caller's safe
    default over a formatting slip, which is the worse outcome.

    Leading content is NOT tolerated. Prose arriving before the object is the
    model talking about the task instead of doing it, which is the shape of
    the ticket #8945 failure, and it fails.
    """
    cleaned = strip_code_fence(text)
    try:
        value = json.loads(cleaned)
    except (ValueError, TypeError) as exc:
        value, trailing = _leading_json_object(cleaned)
        if value is None:
            return GuardResult("json_shape", False, f"not valid JSON ({exc})")
        return GuardResult(
            "json_shape", True,
            f"parsed, but {trailing} characters of trailing content followed "
            "the object",
            value=value,
        )
    if not isinstance(value, dict):
        return GuardResult(
            "json_shape", False,
            f"expected a JSON object, got {type(value).__name__}",
        )
    return GuardResult("json_shape", True, value=value)


def _leading_json_object(text: str) -> tuple[dict | None, int]:
    """Decode a JSON object at the very start of `text`.

    Returns (object, trailing_char_count), or (None, 0) when the text does not
    begin with a complete object.
    """
    if not text.startswith("{"):
        return (None, 0)
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return (None, 0)
    if not isinstance(value, dict):
        return (None, 0)
    return (value, len(text) - end)


@guard("non_empty")
def _non_empty(text: str, ctx: dict) -> GuardResult:
    """Output is not blank."""
    if len((text or "").strip()) < 2:
        return GuardResult("non_empty", False, "output is empty")
    return GuardResult("non_empty", True)


@guard("max_length")
def _max_length(text: str, ctx: dict) -> GuardResult:
    """Output is within `max_length_chars` from the run context.

    The limit lives in context rather than in the guard so one guard serves
    every agent. Declaring the guard without supplying the number fails, so a
    forgotten limit cannot silently pass.
    """
    limit = int(ctx.get("max_length_chars") or 0)
    if limit <= 0:
        return GuardResult(
            "max_length", False,
            "max_length declared but max_length_chars was not supplied",
        )
    size = len((text or "").strip())
    if size > limit:
        return GuardResult(
            "max_length", False, f"{size} chars exceeds limit of {limit}"
        )
    return GuardResult("max_length", True)
