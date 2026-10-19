"""The model bench: the new tiers, effort, and refusal fallbacks.

Sammy, 2026-10-18: the video team's strategy runs on the most capable model
and its writing on the current Opus, without moving every senior agent in
the support app with it.
"""

from types import SimpleNamespace

from vomeos import config, runner, tiers
from vomeos.manifest import EFFORT_LEVELS, ModelSpec


def test_the_new_tiers_resolve_and_senior_is_untouched():
    assert tiers.resolve("executive") == "claude-fable-5-1"
    assert tiers.resolve("premium") == "claude-opus-5-5"
    assert tiers.resolve("current") == "claude-sonnet-5-5"
    # The support app's senior agents did not move.
    assert tiers.resolve("senior") == config.MODEL_SENIOR


class _Client:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="{}")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def _manifest(effort=None, max_tokens=1000):
    return SimpleNamespace(
        qualified="content.test",
        model=ModelSpec(tier="premium", max_tokens=max_tokens, effort=effort),
        limits=SimpleNamespace(max_retries=0, timeout_seconds=30))


def test_effort_and_fallbacks_reach_the_api(monkeypatch):
    client = _Client()
    monkeypatch.setattr(runner, "_client", client)
    runner._call_model(_manifest(effort="high"), "sys", "user",
                       "claude-opus-5-5")
    sent = client.calls[0]
    assert sent["output_config"] == {"effort": "high"}
    assert sent["extra_body"] == {"fallbacks": "default"}
    assert sent["extra_headers"]["anthropic-beta"] == \
        "server-side-fallback-2026-07-01"
    assert "temperature" not in sent


def test_older_models_get_neither(monkeypatch):
    client = _Client()
    monkeypatch.setattr(runner, "_client", client)
    runner._call_model(_manifest(), "sys", "user", "claude-haiku-4-5")
    sent = client.calls[0]
    assert "output_config" not in sent and "extra_body" not in sent


def test_effort_levels_are_the_apis():
    assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")


def test_senior_agents_keep_high_effort_on_opus_5_5(monkeypatch):
    """Opus 5.5 defaults to medium; the senior agents were tuned at Opus
    5's high (2026-10-19), so their tier pins it unless a manifest says
    otherwise."""
    client = _Client()
    monkeypatch.setattr(runner, "_client", client)
    senior = SimpleNamespace(
        qualified="customer_success.test",
        model=ModelSpec(tier="senior", max_tokens=1000),
        limits=SimpleNamespace(max_retries=0, timeout_seconds=30))
    runner._call_model(senior, "sys", "user", "claude-opus-5-5")
    assert client.calls[0]["output_config"] == {"effort": "high"}
    own = SimpleNamespace(
        qualified="customer_success.test",
        model=ModelSpec(tier="senior", max_tokens=1000, effort="medium"),
        limits=SimpleNamespace(max_retries=0, timeout_seconds=30))
    runner._call_model(own, "sys", "user", "claude-opus-5-5")
    assert client.calls[1]["output_config"] == {"effort": "medium"}
