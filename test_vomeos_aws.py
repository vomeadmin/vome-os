"""
test_vomeos_aws.py

The CloudWatch connector: the request signer, the read operations, and the
two connector tools built on them. AWS itself is faked at httpx, so every
byte the connector would send is checked as it would leave.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from mcp_connector import feature_requests, tools
from vomeos.integrations import aws
from vomeos.integrations import base

SAM = "s.fagen@vomevolunteer.co"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    )
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_REGION", "ca-central-1")
    monkeypatch.delenv("MCP_TRIAGE_EMAILS", raising=False)
    monkeypatch.setattr(aws.time, "sleep", lambda s: None)


class _Response:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeAws:
    """Answers by X-Amz-Target, records every request."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, content=None, **kwargs):
        target = headers["x-amz-target"].split(".", 1)[1]
        body = json.loads(content)
        self.calls.append({
            "url": url, "target": target, "body": body,
            "headers": headers, "raw": content, "kwargs": kwargs,
        })
        answer = self.answers.get(target, {})
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, tuple):
            return _Response(*answer)
        return _Response(200, answer)

    def targets(self) -> list[str]:
        return [c["target"] for c in self.calls]


@pytest.fixture
def fake(monkeypatch):
    def install(answers: dict) -> FakeAws:
        faker = FakeAws(answers)
        monkeypatch.setattr(base.httpx, "request", faker)
        return faker
    return install


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------

def test_signature_matches_botocore():
    # Produced by botocore's SigV4Auth for this exact request on 2026-10-31.
    headers = aws.sign(
        host="monitoring.ca-central-1.amazonaws.com",
        body=b'{"MaxRecords":1}',
        headers={
            "Content-Type": "application/x-amz-json-1.0",
            "X-Amz-Target": "GraniteServiceVersion20100801.DescribeAlarms",
        },
        service="monitoring",
        region_name="ca-central-1",
        key_id="AKIDEXAMPLE",
        secret="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        now=datetime(2026, 10, 31, 22, 0, 0, tzinfo=timezone.utc),
    )
    assert headers["x-amz-date"] == "20261031T220000Z"
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261031/ca-central-1/"
        "monitoring/aws4_request, SignedHeaders=content-type;host;"
        "x-amz-date;x-amz-target, Signature="
        "7fc51dcf7b25c50b24daf6f4b7ebda19c30ff43770fa23080b2291c2b90afa90"
    )


def test_the_signed_bytes_are_the_bytes_sent(fake):
    faker = fake({"DescribeAlarms": {"MetricAlarms": []}})
    aws.CloudWatch().alarms()
    call = faker.calls[0]
    assert call["url"] == "https://monitoring.ca-central-1.amazonaws.com/"
    assert call["kwargs"]["json"] is None
    assert call["headers"]["content-type"] == "application/x-amz-json-1.0"
    assert "AKIDEXAMPLE/" in call["headers"]["authorization"]
    assert "EXAMPLEKEY" not in json.dumps(call["headers"])


def test_no_keys_means_no_call(monkeypatch, fake):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    faker = fake({})
    result = aws.CloudWatch().alarms()
    assert not result.ok and "not set" in result.error
    assert faker.calls == []
    assert aws.describe()["status"].startswith("not set")


# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------

def test_alarms_follow_every_page(fake):
    pages = iter([
        {"MetricAlarms": [{"AlarmName": "a", "StateValue": "ALARM",
                           "MetricName": "m", "StateUpdatedTimestamp": 0}],
         "NextToken": "t2"},
        {"MetricAlarms": [{"AlarmName": "b", "StateValue": "ALARM",
                           "Metrics": [{"Id": "ratio"}]}]},
    ])
    faker = fake({"DescribeAlarms": lambda body: next(pages)})
    result = aws.CloudWatch().alarms(state="ALARM")
    assert result.ok
    assert [a["name"] for a in result.data] == ["a", "b"]
    assert result.data[1]["metric"] == "metric math"
    assert result.data[0]["since"] == "1970-01-01 00:00:00 UTC"
    assert faker.calls[0]["body"]["StateValue"] == "ALARM"
    assert faker.calls[1]["body"]["NextToken"] == "t2"


def test_an_aws_error_is_a_failed_result_not_an_exception(fake):
    fake({"DescribeAlarms": (400, {"__type": "AccessDenied",
                                   "message": "not authorized"})})
    result = aws.CloudWatch().alarms()
    assert not result.ok
    assert "400" in result.error and "not authorized" in result.error


def _metric(name, **dims):
    return {
        "Namespace": aws.RABBITMQ_NAMESPACE, "MetricName": name,
        "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
    }


def test_rabbitmq_snapshot_reads_queues_and_headroom(fake):
    listed = [
        _metric("rabbitmq_detailed_queue_messages_ready",
                vhost="ezntpafr_vhost", queue="high_priority"),
        _metric("rabbitmq_detailed_queue_consumers",
                vhost="ezntpafr_vhost", queue="high_priority"),
        _metric("rabbitmq_process_resident_memory_bytes", job="rabbitmq_node"),
        _metric("rabbitmq_resident_memory_limit_bytes", job="rabbitmq_node"),
        _metric("something_else", job="rabbitmq_node"),
    ]

    def data(body):
        ids = [q["Id"] for q in body["MetricDataQueries"]]
        values = {"m0": [5, 40, 12], "m1": [6, 6, 0],
                  "m2": [1e9, 1e9, 2e9], "m3": [4e10, 4e10, 4e10]}
        return {"MetricDataResults": [
            {"Id": i, "Timestamps": [60, 120, 180], "Values": values[i]}
            for i in ids
        ]}

    faker = fake({"ListMetrics": {"Metrics": listed}, "GetMetricData": data})
    result = aws.CloudWatch().rabbitmq(minutes=30)
    assert result.ok, result.error
    queue = result.data["queues"][0]
    assert queue["queue"] == "high_priority"
    assert queue["waiting"]["now"] == 12 and queue["waiting"]["max"] == 40
    assert queue["workers"]["now"] == 0 and queue["workers"]["min"] == 0
    assert result.data["broker"]["memory"]["ratio"] == 0.05
    # The unrelated metric is never asked for.
    sent = faker.calls[-1]["body"]["MetricDataQueries"]
    assert len(sent) == 4


# ---------------------------------------------------------------------------
# CloudWatch Logs
# ---------------------------------------------------------------------------

def test_text_is_quoted_so_it_cannot_change_the_query():
    query = aws.insights_query('a" | display @message\nb', limit=999)
    assert 'like "a\\" | display @message b"' in query
    assert query.endswith("| limit 200")
    assert "/(?i)(error" in aws.insights_query("")


def test_search_resolves_groups_then_waits_for_the_query(fake):
    polls = iter([
        {"status": "Running"},
        {"status": "Complete",
         "results": [[{"field": "@message", "value": "Traceback"},
                      {"field": "@ptr", "value": "x"}]],
         "statistics": {"recordsMatched": 1.0}},
    ])
    faker = fake({
        "DescribeLogGroups": lambda body: {"logGroups": [
            {"logGroupName": body["logGroupNamePrefix"] + "/worker"}
        ]},
        "StartQuery": {"queryId": "q1"},
        "GetQueryResults": lambda body: next(polls),
    })
    result = aws.CloudWatchLogs().search("", app="core", minutes=15)
    assert result.ok, result.error
    assert result.data["status"] == "Complete"
    assert result.data["rows"] == [{"@message": "Traceback"}]
    start = next(c for c in faker.calls if c["target"] == "StartQuery")
    assert start["body"]["logGroupNames"] == ["celery-production/worker"]
    assert start["body"]["endTime"] - start["body"]["startTime"] == 900
    assert faker.calls[0]["url"] == "https://logs.ca-central-1.amazonaws.com/"
    assert faker.calls[0]["headers"]["content-type"] == (
        "application/x-amz-json-1.1"
    )


def test_a_slow_query_is_stopped_and_partial_results_returned(
    monkeypatch, fake,
):
    # Every read of the clock moves it 10 seconds, so the 5 second budget is
    # spent by the first poll.
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(aws.time, "monotonic", lambda: next(clock))
    faker = fake({
        "StartQuery": {"queryId": "q1"},
        "GetQueryResults": {"status": "Running", "results": []},
        "StopQuery": {"success": True},
    })
    result = aws.CloudWatchLogs().query(["g"], "q", wait_seconds=5)
    assert result.ok
    assert result.data["status"] == "Timeout (partial)"
    assert "StopQuery" in faker.targets()


def test_unknown_app_is_refused_without_a_call(fake):
    faker = fake({})
    result = aws.CloudWatchLogs().search("x", app="mobile")
    assert not result.ok and "unknown app" in result.error
    assert faker.calls == []


# ---------------------------------------------------------------------------
# connector tools
# ---------------------------------------------------------------------------

def test_only_sam_reads_infrastructure(fake):
    faker = fake({})
    with pytest.raises(feature_requests.RequestError):
        tools.celery_logs(text="", app="all", minutes=60, limit=10,
                          caller="colleague@vomevolunteer.co")
    with pytest.raises(feature_requests.RequestError):
        tools.celery_health(minutes=60, caller="colleague@vomevolunteer.co")
    assert faker.calls == []


def test_celery_health_for_sam(fake):
    fake({
        "ListMetrics": {"Metrics": []},
        "DescribeAlarms": {"MetricAlarms": [
            {"AlarmName": "rabbitmq-core-high_priority-no-workers",
             "StateValue": "ALARM"}
        ]},
    })
    result = tools.celery_health(minutes=60, caller=SAM)
    assert result["alarms_firing"][0]["name"].endswith("no-workers")
    assert result["rabbitmq"]["queues"] == []


def test_a_cloudwatch_failure_is_a_readable_refusal(fake):
    fake({"ListMetrics": (403, {"message": "denied"})})
    with pytest.raises(feature_requests.RequestError, match="denied"):
        tools.celery_health(minutes=60, caller=SAM)


def test_describe_checks_each_permission(fake):
    fake({
        "DescribeAlarms": {"MetricAlarms": []},
        "ListMetrics": {"Metrics": []},
        "DescribeLogGroups": (400, {"message": "not authorized"}),
    })
    status = aws.describe()
    assert status["alarms"] == "ok" and status["metrics"] == "ok"
    assert status["worker_logs"].startswith("failing")
    assert "EXAMPLEKEY" not in json.dumps(status)
    assert aws.describe(live=False) == {
        "region": "ca-central-1", "status": "configured",
    }
