"""
vomeos/integrations/vome_studio.py

The connector to Vome Studio, the video render engine.

Studio takes a storyboard (every word of a video, and no timings) and returns
MP4s in four platform formats. It is a separate repository and will be a
separate Railway service. The OS asks it to build things over HTTP and never
imports it, the same rule that governs every connector in this package.

WHAT STUDIO IS, AND IS NOT

It is deliberately dumb. It assembles videos from templates that already
exist. It does not invent new UI screens, and asking it to is not a thing the
API can express. A feature that needs a genuinely new kind of screen is a
design task for a person, not a request this connector can make.

The storyboard is the contract, specified by storyboard.schema.json in that
repository. The important property: it contains NO timings. Studio measures
the real voice track and derives every start time, scene boundary and cue
from it. So the OS never sends a timestamp and never has to reason about
pacing; it sends words and gets back a finished video with a runtime.

THE PHASE 2 CAVEAT

Studio has no HTTP endpoint yet. Until it does, `configured()` returns False
and every operation reports that honestly rather than pretending. The shapes
below are the contract Phase 2 will serve, written now so the agent and the
Slack workflow can be built against something real.

For local work before then, the same operations are available as a CLI in the
Studio repository, which emits exactly the `build` payload documented here:

    py scripts/produce.py --dry-run
    py scripts/produce.py --engine elevenlabs

Environment:

    VOME_STUDIO_API_URL   base URL of the render service
    VOME_STUDIO_API_KEY   shared secret
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from vomeos import config
from vomeos.integrations.base import Connector, IntegrationResult


def _base_url() -> str:
    return os.environ.get("VOME_STUDIO_API_URL", "")


def _api_key() -> str:
    return os.environ.get("VOME_STUDIO_API_KEY", "")


@dataclass
class VomeStudio(Connector):
    """Build branded feature videos from a storyboard."""

    system: str = "vome-studio"
    base_url: str = field(default_factory=_base_url)
    timeout: int = field(default=config.INTEGRATION_TIMEOUT_SECONDS)

    def configured(self) -> bool:
        return bool(self.base_url) and bool(_api_key())

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Studio-Api-Key": _api_key(),
        }

    # -----------------------------------------------------------------
    # Operations. One method per thing the OS is allowed to ask for.
    # -----------------------------------------------------------------

    def contract(self) -> IntegrationResult:
        """What a storyboard must look like, from Studio itself.

        `{"schema", "example", "required_cues", "templates"}`. The example is
        an approved storyboard: each template draws one specific mock, so a
        new storyboard keeps its structure and `on_screen` keys and changes
        the words and mock content. Free.
        """
        return self._call("contract", "GET", "/v1/contract")

    def validate(self, storyboard: dict) -> IntegrationResult:
        """Check a storyboard and estimate its runtime. Builds nothing.

        Free, fast, and spends no credits, so there is no reason to post a
        script for review without calling this first. It catches the things
        that would otherwise be found after the money was spent: em dashes
        (the house copy rule), duplicate cues, unknown templates, empty lines.

        Returns `{"ok", "estimated_runtime", "lines", "words", "warnings"}`.
        """
        return self._call(
            "validate",
            "POST",
            "/v1/validate",
            json_body={"storyboard": storyboard},
        )

    def build(self, storyboard: dict, engine: str = "elevenlabs",
              aspects: list[str] | None = None) -> IntegrationResult:
        """Build the video. THIS SPENDS MONEY and takes minutes.

        Only call it on an explicit human greenlight. Everything before that
        point is free, and the whole script-review loop exists so that this
        call is made once per approved script rather than once per idea.

        `engine` "local" uses a free draft voice; it is for checking pacing,
        never for publishing. Returns the report documented in the module
        docstring: folder, one entry per format, runtime, and warnings worth
        repeating to a human (over-length, a missing audio stream, a draft
        voice that must not be published).
        """
        body: dict = {"storyboard": storyboard, "engine": engine}
        if aspects:
            body["aspects"] = aspects
        return self._call("build", "POST", "/v1/build", json_body=body)

    def get_build(self, build_id: str) -> IntegrationResult:
        """Poll a build started earlier.

        A four-format render takes minutes, which is longer than an HTTP
        request should be held open, so Phase 2 returns a build id and the
        caller polls. Reports `{"status": "running"|"done"|"failed"}` plus
        the same payload as `build` once it is done.
        """
        return self._call("get_build", "GET", "/v1/builds/%s" % build_id)
