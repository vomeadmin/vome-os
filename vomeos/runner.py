"""
vomeos/runner.py

The one path every agent call goes through.

WHY THIS EXISTS
---------------
There are ~30 `messages.create` call sites in this repo and no two of them
handle failure the same way. Some catch Exception and return a default, some
let it propagate, none retry, none record what it cost, and every one parses
JSON with its own private regex for stripping code fences.

`run_agent()` replaces all of that with a single sequence that is the same for
every agent in every division:

    manifest -> system prompt -> model call (retry) -> guards -> trace

The important property is that the caller cannot skip a step. Guards are not
an argument the caller may forget to pass, they are a field on the manifest.
The trace is not a logging call the caller has to remember, it happens here.
That is what makes adding the fiftieth agent as safe as the first.

FAILURE POLICY
--------------
A run comes back as one of three statuses and the caller must branch on it:

    ok        the output passed every guard. Use it.
    blocked   the model answered but a guard rejected it. Route to a human.
              Never fall back to sending it anyway.
    error     the call failed after retries. Use your own safe default.

`AgentResult.ok` is True only for "ok", so `if result.ok:` is the correct
guard in calling code and the unsafe path takes deliberate effort to write.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import anthropic

from vomeos import tiers, trace
from vomeos.composer import compose_system_prompt, render_context
from vomeos.guards import GuardResult, run_guards
from vomeos.manifest import AgentManifest
from vomeos.registry import get_agent
from vomeos.text import strip_code_fence

# One shared client. The SDK is thread safe and each handler building its own
# was pure duplication.
_client = anthropic.Anthropic()

# Errors worth trying again. A 429 or a 5xx is the API having a moment; a 400
# is our prompt being wrong and retrying it just costs money twice.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"


@dataclass
class AgentResult:
    agent: str
    status: str
    run_id: str
    text: str = ""
    # Parsed object for agents whose manifest declares output = "json".
    data: dict | None = None
    guards: list[GuardResult] = field(default_factory=list)
    error: str = ""
    model: str = ""
    tier: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    def failed_guards(self) -> list[GuardResult]:
        return [g for g in self.guards if not g.ok]

    def why(self) -> str:
        """One line a human can read in Slack."""
        if self.ok:
            return "passed"
        if self.status == STATUS_BLOCKED:
            return "; ".join(
                f"{g.name}: {g.reason}" for g in self.failed_guards()
            ) or "blocked by a guard"
        return self.error or "call failed"

    def get(self, key: str, default=None):
        """Read a field from a JSON agent's output without a None check."""
        return (self.data or {}).get(key, default)


# Sampling parameters that newer models no longer accept. A manifest may
# still declare one, because a tier it targets today might honour it, and the
# author's intent ("this decision must be stable") is worth recording either
# way. Rather than maintain a list of which model takes which knob, which is
# exactly the kind of fact that goes stale and broke us before, the runner
# drops the parameter when the API says it is deprecated and retries once.
_DEPRECATED_PARAM_RE = re.compile(
    r"`?(temperature|top_p|top_k)`?\s+is\s+deprecated", re.IGNORECASE
)


def _call_model(
    manifest: AgentManifest, system: str, user: str, model: str
) -> tuple[str, int, int, int]:
    """Call the API with backoff.

    Returns (text, input_tokens, output_tokens, attempts).
    """
    last: Exception | None = None
    attempts = manifest.limits.max_retries + 1
    send_temperature = manifest.model.temperature is not None
    for attempt in range(1, attempts + 1):
        try:
            kwargs = {
                "model": model,
                "max_tokens": manifest.model.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "timeout": manifest.limits.timeout_seconds,
            }
            if send_temperature:
                kwargs["temperature"] = manifest.model.temperature
            response = _client.messages.create(**kwargs)
            text = "".join(
                block.text for block in response.content
                if getattr(block, "type", "") == "text"
            ).strip()
            usage = getattr(response, "usage", None)
            return (
                text,
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
                attempt,
            )
        except _RETRYABLE as exc:
            last = exc
            if attempt < attempts:
                # 1s, 2s, 4s. Short on purpose: these run inside webhook
                # handling, and a caller waiting a minute is its own outage.
                time.sleep(2 ** (attempt - 1))
        except anthropic.BadRequestError as exc:
            # A sampling parameter this model no longer accepts. Drop it and
            # try once more, rather than failing an otherwise valid call over
            # a knob. Anything else in a 400 is our prompt being wrong, and
            # retrying that just costs money twice.
            if send_temperature and _DEPRECATED_PARAM_RE.search(str(exc)):
                print(
                    f"[VOMEOS] {model} does not accept temperature; "
                    "retrying without it"
                )
                send_temperature = False
                last = exc
                continue
            raise
        except Exception as exc:  # not retryable, fail immediately
            raise exc
    raise last if last else RuntimeError("model call failed with no error")


def run_agent(
    qualified: str,
    context: dict,
    *,
    subject_type: str = "",
    subject_id: str = "",
    guard_context: dict | None = None,
) -> AgentResult:
    """Run one agent against one piece of work.

    `context` is data, never instructions: the agent's instructions live in its
    charter on disk. `subject_type` / `subject_id` tie the run to the thing it
    was about ("zoho_ticket", "8945") so the trace can answer "what did we
    decide about this ticket, and which agent decided it".
    """
    manifest = get_agent(qualified)
    run_id = trace.new_run_id()
    model = tiers.resolve(manifest.model.tier)

    system = compose_system_prompt(manifest)
    user = render_context(manifest, context)

    started = time.monotonic()
    try:
        text, in_tokens, out_tokens, attempts = _call_model(
            manifest, system, user, model
        )
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        result = AgentResult(
            agent=qualified, status=STATUS_ERROR, run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
            model=model, tier=manifest.model.tier,
            duration_ms=duration, attempts=manifest.limits.max_retries + 1,
        )
        print(f"[RUNTIME] {qualified} errored: {result.error}")
        _persist(manifest, result, subject_type, subject_id)
        return result

    duration = int((time.monotonic() - started) * 1000)

    # Guards see the raw text. `agent` is passed through so a guard that logs
    # (client_message prints a BLOCKED line) names who produced the draft.
    gctx = {"agent": qualified, **(guard_context or {})}
    guard_results = run_guards(manifest.guards.output, text, gctx)

    data: dict | None = None
    for gr in guard_results:
        if gr.name == "json_shape" and gr.ok and isinstance(gr.value, dict):
            data = gr.value
    if data is None and manifest.job.output == "json":
        # A JSON agent that did not declare json_shape still gets parsed, but
        # the onboarding gate flags the missing guard so this stays rare.
        try:
            import json as _json
            parsed = _json.loads(strip_code_fence(text))
            data = parsed if isinstance(parsed, dict) else None
        except ValueError:
            data = None

    passed = all(g.ok for g in guard_results)
    result = AgentResult(
        agent=qualified,
        status=STATUS_OK if passed else STATUS_BLOCKED,
        run_id=run_id,
        text=text,
        data=data,
        guards=guard_results,
        model=model,
        tier=manifest.model.tier,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        duration_ms=duration,
        attempts=attempts,
    )
    if not passed:
        print(f"[RUNTIME] {qualified} blocked: {result.why()}")
    _persist(manifest, result, subject_type, subject_id)
    return result


def _persist(
    manifest: AgentManifest,
    result: AgentResult,
    subject_type: str,
    subject_id: str,
) -> None:
    trace.record(
        run_id=result.run_id,
        agent=result.agent,
        division=manifest.identity.division,
        tier=result.tier,
        model=result.model,
        status=result.status,
        subject_type=subject_type,
        subject_id=subject_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        duration_ms=result.duration_ms,
        attempts=result.attempts,
        guards=[
            {"name": g.name, "ok": g.ok, "reason": g.reason}
            for g in result.guards
        ],
        # The decision is stored for JSON agents. Free text is not, so a
        # client draft does not end up duplicated in a second table.
        output=result.data or {},
        error=result.error,
    )
