"""
test_sentry_gate.py

The gate has two failure modes and only one of them is obvious.

The obvious one is letting noise through, which costs a model call and, worse,
a Slack message nobody wanted.

The dangerous one is dropping everything. A gate that silently eats its whole
input looks exactly like a quiet week, and that is the failure that ran the
ClickUp webhook outage for most of a day. The environment tests below are the
ones that matter most, because an `issue.created` payload carries no
environment field at all, so "drop what is not production" would drop
everything, forever, invisibly.
"""

import sentry_gate as gate
import sentry_projects
from vomeos.integrations.sentry import IssueSignal


def _signal(**kwargs) -> IssueSignal:
    defaults = {
        "issue_id": "1001",
        "project": "prod-vome",
        "title": "AttributeError: 'NoneType' object has no attribute 'site'",
        "culprit": "opportunity_app/views.py in reserve_shift",
        "level": "error",
        "environment": "",
        "exception_type": "AttributeError",
    }
    defaults.update(kwargs)
    return IssueSignal(**defaults)


def _clear(monkeypatch, **env):
    for name in (
        "SENTRY_PROJECT_ALLOWLIST",
        "SENTRY_ENVIRONMENTS",
        "SENTRY_REQUIRE_ENVIRONMENT",
        "SENTRY_IGNORE_EXCEPTION_TYPES",
        "SENTRY_TRIAGE_DEV_PROJECTS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_real_production_error_passes(monkeypatch):
    _clear(monkeypatch)
    assert gate.check(_signal()).passed


# ---------------------------------------------------------------------------
# Level
# ---------------------------------------------------------------------------

def test_warnings_are_dropped(monkeypatch):
    # A warning is a backlog, not an incident. A pipeline that triages
    # warnings never gets to the errors.
    _clear(monkeypatch)
    decision = gate.check(_signal(level="warning"))
    assert decision.blocked
    assert decision.rule == "level"


def test_fatal_passes(monkeypatch):
    _clear(monkeypatch)
    assert gate.check(_signal(level="fatal")).passed


def test_an_unrecognised_level_passes(monkeypatch):
    # If Sentry invents a new level name, the safe reading is "important".
    # Dropping it would be silent data loss.
    _clear(monkeypatch)
    assert gate.check(_signal(level="catastrophe")).passed


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

def test_all_five_real_projects_are_mapped(monkeypatch):
    # The five Sentry projects we actually run. If one is renamed or a sixth
    # appears, this is where it gets noticed.
    _clear(monkeypatch)
    for slug in (
        "prod-vome",
        "dev-vome-app",
        "prod-volunteer-database",
        "dev-volunteer-database",
        "vome-2j",
    ):
        assert sentry_projects.route(slug) is not None, slug


def test_the_two_production_backends_and_the_frontend_are_triaged(monkeypatch):
    _clear(monkeypatch)
    assert gate.check(_signal(project="prod-vome")).passed
    assert gate.check(_signal(project="prod-volunteer-database")).passed
    assert gate.check(_signal(project="vome-2j")).passed


def test_dev_projects_are_dropped(monkeypatch):
    # Dev and prod are separate PROJECTS here, not environments on one
    # project. The project table is the production filter, and a bug showing
    # up in dev is the system working.
    _clear(monkeypatch)
    for slug in ("dev-vome-app", "dev-volunteer-database"):
        decision = gate.check(_signal(project=slug))
        assert decision.blocked, slug
        assert decision.rule == "project_dev"


def test_dev_projects_can_be_opted_into(monkeypatch):
    _clear(monkeypatch, SENTRY_TRIAGE_DEV_PROJECTS="true")
    assert gate.check(_signal(project="dev-vome-app")).passed


def test_an_unmapped_project_is_dropped_and_named(monkeypatch):
    # Not silently. A new Sentry project nobody mapped is a configuration
    # gap, and the daily report calls it out so it gets mapped.
    _clear(monkeypatch)
    decision = gate.check(_signal(project="brand-new-project"))
    assert decision.blocked
    assert decision.rule == "project_unmapped"
    assert "sentry_projects.py" in decision.detail


def test_dev_and_prod_read_the_same_repo_at_different_branches(monkeypatch):
    # The reason `ref` is per project rather than per repository. A dev
    # traceback read against main is a traceback read against code that is
    # not running, with line numbers that do not match.
    _clear(monkeypatch)
    dev = sentry_projects.route("dev-vome-app")
    prod = sentry_projects.route("prod-vome")
    assert dev.repo == prod.repo
    assert dev.ref == "development"
    # Production is master, not main. git reports origin/HEAD -> origin/main
    # on this repo, which is the default branch pointer and not the branch
    # that is deployed. Confirmed by Sam.
    assert prod.ref == "master"


def test_the_mobile_app_default_branch_is_master(monkeypatch):
    # VomeApp's default branch is master, not main. Reading main would 404.
    _clear(monkeypatch)
    assert sentry_projects.route("vome-2j").ref == "master"


def test_starting_config_triages_only_the_dev_backend(monkeypatch):
    # The phase 1 starting point: one project, the dev backend, on the
    # development branch. The allowlist overrides the production default,
    # so SENTRY_TRIAGE_DEV_PROJECTS is not needed as well.
    _clear(monkeypatch, SENTRY_PROJECT_ALLOWLIST="dev-vome-app")
    assert gate.check(_signal(project="dev-vome-app")).passed
    for other in ("prod-vome", "prod-volunteer-database", "vome-2j"):
        assert gate.check(_signal(project=other)).blocked, other
    assert sentry_projects.repos_in_scope() == (
        "vomedjango-restored-core-app",
    )


def test_the_allowlist_can_narrow_to_one_project_while_tuning(monkeypatch):
    _clear(monkeypatch, SENTRY_PROJECT_ALLOWLIST="prod-vome")
    assert gate.check(_signal(project="prod-vome")).passed
    decision = gate.check(_signal(project="prod-volunteer-database"))
    assert decision.blocked
    assert decision.rule == "project_excluded"


# ---------------------------------------------------------------------------
# Environment. The one that could silently kill the pipeline.
# ---------------------------------------------------------------------------

def test_unknown_environment_passes_by_default(monkeypatch):
    # An issue.created payload has no environment field. If unknown meant
    # "drop", the pipeline would process nothing and look healthy doing it.
    _clear(monkeypatch)
    assert gate.check(_signal(environment="")).passed


def test_unknown_environment_can_be_tightened_once_it_is_understood(monkeypatch):
    _clear(monkeypatch, SENTRY_REQUIRE_ENVIRONMENT="true")
    decision = gate.check(_signal(environment=""))
    assert decision.blocked
    assert decision.rule == "environment_unknown"


def test_staging_is_dropped_when_the_environment_is_known(monkeypatch):
    _clear(monkeypatch)
    decision = gate.check(_signal(environment="staging"))
    assert decision.blocked
    assert decision.rule == "environment"


def test_production_passes(monkeypatch):
    _clear(monkeypatch)
    assert gate.check(_signal(environment="production")).passed


def test_environments_are_configurable(monkeypatch):
    _clear(monkeypatch, SENTRY_ENVIRONMENTS="production,prod-eu")
    assert gate.check(_signal(environment="prod-eu")).passed


# ---------------------------------------------------------------------------
# The noise list
# ---------------------------------------------------------------------------

def test_client_disconnects_are_dropped(monkeypatch):
    _clear(monkeypatch)
    decision = gate.check(_signal(exception_type="BrokenPipeError"))
    assert decision.blocked
    assert decision.rule == "exception_type"


def test_scanner_traffic_is_dropped(monkeypatch):
    _clear(monkeypatch)
    assert gate.check(_signal(exception_type="DisallowedHost")).blocked


def test_the_ignore_list_is_extensible_without_a_deploy(monkeypatch):
    _clear(monkeypatch, SENTRY_IGNORE_EXCEPTION_TYPES="SomeNewNoise")
    assert gate.check(_signal(exception_type="SomeNewNoise")).blocked


def test_resize_observer_noise_is_dropped_by_title(monkeypatch):
    # Its type is generic, so the message is the identifying part.
    _clear(monkeypatch)
    decision = gate.check(
        _signal(
            title="ResizeObserver loop limit exceeded",
            exception_type="Error",
        )
    )
    assert decision.blocked
    assert decision.rule == "title"


def test_browser_extension_errors_are_dropped_by_culprit(monkeypatch):
    _clear(monkeypatch)
    decision = gate.check(
        _signal(
            culprit="chrome-extension://abcdefg/inject.js",
            exception_type="TypeError",
        )
    )
    assert decision.blocked
    assert decision.rule == "culprit"


def test_node_modules_errors_are_dropped(monkeypatch):
    _clear(monkeypatch)
    assert gate.check(
        _signal(culprit="/app/node_modules/some-lib/index.js")
    ).blocked


def test_our_own_code_is_never_dropped_by_the_culprit_rule(monkeypatch):
    # The paired assertion. The culprit patterns must not match our source.
    _clear(monkeypatch)
    for culprit in (
        "opportunity_app/views.py in reserve_shift",
        "src/components/ScheduleGrid.jsx",
        "vomeos/runner.py in run_agent",
    ):
        assert gate.check(_signal(culprit=culprit)).passed, culprit


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_every_block_names_a_rule(monkeypatch):
    # The daily report counts by rule. A blocked decision with no rule name
    # is invisible in the report and therefore untunable.
    _clear(monkeypatch)
    blocked = [
        _signal(level="info"),
        _signal(exception_type="Http404"),
        _signal(title="Loading chunk 12 failed", exception_type="Error"),
        _signal(culprit="moz-extension://x/y.js"),
    ]
    for signal in blocked:
        decision = gate.check(signal)
        assert decision.blocked
        assert decision.rule
        assert decision.detail


def test_describe_reports_the_live_configuration(monkeypatch):
    _clear(monkeypatch, SENTRY_PROJECT_ALLOWLIST="prod-vome,vome-2j")
    described = gate.describe()
    assert described["projects"] == ["prod-vome", "vome-2j"]
    assert described["environments"] == ["production"]


def test_the_routing_table_points_at_real_repositories(monkeypatch):
    # The exact Bitbucket and GitHub slugs. A typo here means the analyst
    # reads nothing and reports "could not reach the repository".
    _clear(monkeypatch)
    assert sentry_projects.route("prod-vome").qualified_repo == (
        "vomedjango/vomedjango-restored-core-app"
    )
    assert sentry_projects.route("prod-volunteer-database").qualified_repo == (
        "vomedjango/vomedjango-database-app"
    )
    assert sentry_projects.route("vome-2j").qualified_repo == (
        "samfagen15/VomeApp"
    )


def test_repos_in_scope_is_what_code_repos_must_contain(monkeypatch):
    _clear(monkeypatch)
    assert sentry_projects.repos_in_scope() == (
        "VomeApp",
        "vomedjango-database-app",
        "vomedjango-restored-core-app",
    )


def test_backend_and_mobile_route_to_different_channels(monkeypatch):
    _clear(monkeypatch)
    backend = sentry_projects.route("prod-vome")
    mobile = sentry_projects.route("vome-2j")
    assert backend.channel_env == "SLACK_CHANNEL_ENG_BACKEND"
    assert mobile.channel_env == "SLACK_CHANNEL_ENG_FRONTEND"
    assert backend.host == "bitbucket"
    assert mobile.host == "github"
    assert mobile.stack == sentry_projects.MOBILE


def test_the_only_frontend_project_is_the_mobile_app(monkeypatch):
    # vome-react, the admin web client, has NO Sentry project. If one is ever
    # added this test fails and the routing table needs a new row, which is
    # the point: the gap should not be forgotten.
    _clear(monkeypatch)
    assert "vome-react" not in sentry_projects.repos_in_scope()
    assert "vomeadmin/vome-react" in (
        sentry_projects.describe()["uninstrumented_repos"]
    )


def test_mobile_connectivity_noise_is_dropped(monkeypatch):
    # The largest source of mobile noise: the handset lost signal. Not a bug.
    _clear(monkeypatch)
    for title in (
        "Error: Network request failed",
        "The Internet connection appears to be offline.",
        "The network connection was lost.",
        "The request timed out.",
    ):
        decision = gate.check(_signal(project="vome-2j", title=title,
                                      exception_type="Error"))
        assert decision.blocked, title
        assert decision.rule == "title"


def test_cancellation_bugs_are_not_eaten_by_a_noise_rule(monkeypatch):
    # The patterns are substring matches and Vome's domain is full of
    # cancellation. A rule matching a bare "cancelled" would silently
    # swallow real bugs in the shift cancellation paths.
    _clear(monkeypatch)
    for title in (
        "AttributeError: shift cancelled but enrollment still active",
        "Shift cancelled: NoneType has no attribute 'rebooking_policy'",
        "ValueError in cancel_reservation",
    ):
        assert gate.check(
            _signal(project="prod-vome", title=title)
        ).passed, title
