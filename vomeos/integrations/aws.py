"""
vomeos/integrations/aws.py

Read-only access to CloudWatch: alarms, metrics, and the Celery worker logs.

WHAT IS THERE TO READ
---------------------
The Celery prod box runs RabbitMQ, which serves BOTH backends (core on vhost
`ezntpafr_vhost`, django-db on `databaseapp-prod-vhost`). Since 2026-10-31 the
CloudWatch agent on that box publishes per-queue depth and worker counts, and
the broker's memory, disk and file headroom, to the `Vome/RabbitMQ` namespace,
and 25 `rabbitmq-*` alarms post to Slack. Setup lives in core
`docs/rabbitmq-monitoring/`. The same agent has long shipped every worker,
beat and Flower log to the `celery-production/...` (core) and
`databaseapp-celery-production/...` (django-db) log groups.

WHY NOT BOTO3
-------------
The kernel's dependency budget is the seven packages in
`vomeos/requirements.txt`, pinned by test_vomeos_independence.py. Both APIs
used here speak AWS's JSON protocol over HTTPS, so all boto3 would add is
request signing, and Signature Version 4 is forty lines of hmac and hashlib.
The signer is tested against a signature botocore produced for the same
request.

THERE ARE NO WRITE OPERATIONS
-----------------------------
Same rule as `sentry.py`. Every operation below is a Describe, List, Get or a
Logs Insights query (StartQuery is read-only, StopQuery only cancels our own
query). Changing an alarm, a metric or a log group is something a person does.
The IAM user behind the keys (`VomeOS`, policy `VomeOS-Monitoring-ReadOnly`)
cannot do it either, and its log access is limited to the Celery and RabbitMQ
log groups.

ENVIRONMENT
-----------
    AWS_ACCESS_KEY_ID       the VomeOS IAM user's key
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN       only for temporary credentials; normally unset
    AWS_REGION              defaults to ca-central-1
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from vomeos.integrations.base import Connector, IntegrationResult

DEFAULT_REGION = "ca-central-1"

RABBITMQ_NAMESPACE = "Vome/RabbitMQ"

# Log group name prefixes the IAM policy allows, by app. Insights queries take
# exact names, so these are resolved through DescribeLogGroups first.
LOG_GROUP_PREFIXES = {
    "core": ("celery-production",),
    "db": ("databaseapp-celery-production",),
    "rabbitmq": ("/vome/rabbitmq/",),
}

# What "errors" means when a log search names no text.
ERROR_PATTERN = (
    r"/(?i)(error|exception|traceback|workerlost|timelimitexceeded"
    r"|killed|soft time limit)/"
)

# Per-queue metric names, short names first. The agent publishes them with
# the RabbitMQ names.
QUEUE_METRICS = {
    "rabbitmq_detailed_queue_messages_ready": "waiting",
    "rabbitmq_detailed_queue_messages_unacked": "held_by_workers",
    "rabbitmq_detailed_queue_consumers": "workers",
}

# Broker headroom: (used, limit) pairs, published under dimension job.
HEADROOM = {
    "memory": ("rabbitmq_process_resident_memory_bytes",
               "rabbitmq_resident_memory_limit_bytes"),
    "disk_free": ("rabbitmq_disk_space_available_bytes",
                  "rabbitmq_disk_space_available_limit_bytes"),
    "file_descriptors": ("rabbitmq_process_open_fds",
                         "rabbitmq_process_max_fds"),
}

# One Insights query may name at most 50 log groups, and a person reading
# the result does not want more than this many lines.
MAX_LOG_GROUPS = 50
MAX_LOG_ROWS = 200


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def region() -> str:
    return _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or DEFAULT_REGION


def _iso(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (TypeError, ValueError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Signature Version 4
# ---------------------------------------------------------------------------

def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(
    *,
    host: str,
    body: bytes,
    headers: dict[str, str],
    service: str,
    region_name: str,
    key_id: str,
    secret: str,
    session_token: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    """Headers for a signed POST to `https://<host>/` with `body`.

    Only what these JSON APIs need: POST, path "/", no query string. Every
    header passed in is signed, plus host and x-amz-date.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]

    signed = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    if session_token:
        signed["x-amz-security-token"] = session_token
    names = sorted(signed)
    signed_names = ";".join(names)

    canonical = "\n".join([
        "POST",
        "/",
        "",
        "".join(f"{n}:{signed[n]}\n" for n in names),
        signed_names,
        hashlib.sha256(body).hexdigest(),
    ])
    scope = f"{day}/{region_name}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical.encode()).hexdigest(),
    ])

    key = _hmac(f"AWS4{secret}".encode(), day)
    for part in (region_name, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(
        key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()

    out = dict(signed)
    out["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return out


# ---------------------------------------------------------------------------
# The JSON protocol, shared by both services
# ---------------------------------------------------------------------------

@dataclass
class _AwsJson(Connector):
    """One AWS service reached over its JSON protocol."""

    service: str = ""
    target_prefix: str = ""
    json_version: str = "1.0"
    region_name: str = ""

    def __post_init__(self):
        self.region_name = self.region_name or region()
        self.base_url = (
            f"https://{self.service}.{self.region_name}.amazonaws.com"
        )

    def configured(self) -> bool:
        return bool(
            _env("AWS_ACCESS_KEY_ID") and _env("AWS_SECRET_ACCESS_KEY")
        )

    def _op(self, operation: str, body: dict) -> IntegrationResult:
        if not self.configured():
            return IntegrationResult(
                self.system, operation, False,
                error="AWS keys are not set (AWS_ACCESS_KEY_ID, "
                "AWS_SECRET_ACCESS_KEY)",
            )
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers = sign(
            host=self.base_url.split("://", 1)[1],
            body=raw,
            headers={
                "content-type": f"application/x-amz-json-{self.json_version}",
                "x-amz-target": f"{self.target_prefix}.{operation}",
            },
            service=self.service,
            region_name=self.region_name,
            key_id=_env("AWS_ACCESS_KEY_ID"),
            secret=_env("AWS_SECRET_ACCESS_KEY"),
            session_token=_env("AWS_SESSION_TOKEN"),
        )
        return self._call(operation, "POST", "/", headers=headers, content=raw)

    def _pages(
        self, operation: str, body: dict, items_key: str, *,
        token_key: str = "NextToken", max_pages: int = 10,
    ) -> IntegrationResult:
        """Every page of a list operation, concatenated under `items_key`."""
        items: list = []
        body = dict(body)
        result = None
        for _ in range(max_pages):
            result = self._op(operation, body)
            if not result.ok:
                return result
            data = result.data if isinstance(result.data, dict) else {}
            items.extend(data.get(items_key) or [])
            token = data.get(token_key)
            if not token:
                break
            body[token_key] = token
        result.data = {items_key: items}
        return result


# ---------------------------------------------------------------------------
# CloudWatch: alarms and metrics
# ---------------------------------------------------------------------------

def _alarm_row(alarm: dict) -> dict:
    metric = alarm.get("MetricName") or (
        "metric math" if alarm.get("Metrics") else ""
    )
    return {
        "name": alarm.get("AlarmName", ""),
        "state": alarm.get("StateValue", ""),
        "since": _iso(alarm.get("StateUpdatedTimestamp")),
        "reason": alarm.get("StateReason", ""),
        "description": alarm.get("AlarmDescription", ""),
        "namespace": alarm.get("Namespace", ""),
        "metric": metric,
        "dimensions": {
            d.get("Name", ""): d.get("Value", "")
            for d in alarm.get("Dimensions") or []
        },
        "threshold": alarm.get("Threshold"),
        "comparison": alarm.get("ComparisonOperator", ""),
    }


@dataclass
class CloudWatch(_AwsJson):
    system: str = "cloudwatch"
    service: str = "monitoring"
    target_prefix: str = "GraniteServiceVersion20100801"
    json_version: str = "1.0"

    def alarms(
        self, *, prefix: str = "", state: str = "",
    ) -> IntegrationResult:
        """Metric alarms, optionally by name prefix and state
        (OK, ALARM, INSUFFICIENT_DATA)."""
        body: dict = {"AlarmTypes": ["MetricAlarm"], "MaxRecords": 100}
        if prefix:
            body["AlarmNamePrefix"] = prefix
        if state:
            body["StateValue"] = state
        result = self._pages("DescribeAlarms", body, "MetricAlarms")
        if result.ok:
            result.data = [_alarm_row(a) for a in result.data["MetricAlarms"]]
        return result

    def alarm_history(
        self, name: str, *, hours: int = 24,
    ) -> IntegrationResult:
        """State changes of one alarm, newest first."""
        end = time.time()
        result = self._op("DescribeAlarmHistory", {
            "AlarmName": name,
            "HistoryItemType": "StateUpdate",
            "StartDate": end - hours * 3600,
            "EndDate": end,
            "MaxRecords": 50,
            "ScanBy": "TimestampDescending",
        })
        if result.ok:
            result.data = [
                {"time": _iso(item.get("Timestamp")),
                 "summary": item.get("HistorySummary", "")}
                for item in (result.data or {}).get("AlarmHistoryItems") or []
            ]
        return result

    def metrics(self, namespace: str) -> IntegrationResult:
        """Every metric in a namespace with data in the last two weeks."""
        result = self._pages(
            "ListMetrics", {"Namespace": namespace}, "Metrics"
        )
        if result.ok:
            result.data = result.data["Metrics"]
        return result

    def metric_data(
        self, queries: list[dict], *, minutes: int = 60,
    ) -> IntegrationResult:
        """GetMetricData for up to 500 queries over the last `minutes`.

        Data is a dict of query Id to (timestamps, values), oldest first.
        """
        end = time.time()
        result = self._op("GetMetricData", {
            "MetricDataQueries": queries,
            "StartTime": end - minutes * 60,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        })
        if result.ok:
            result.data = {
                r.get("Id", ""): (
                    r.get("Timestamps") or [], r.get("Values") or []
                )
                for r in (result.data or {}).get("MetricDataResults") or []
            }
        return result

    def rabbitmq(self, *, minutes: int = 60) -> IntegrationResult:
        """Every watched queue's waiting, held and worker counts, and the
        broker's headroom, over the last `minutes`.

        Queues are discovered from the namespace, so adding a queue to the
        agent's config needs no change here.
        """
        listed = self.metrics(RABBITMQ_NAMESPACE)
        if not listed.ok:
            return listed

        queries: list[dict] = []
        where: dict[str, tuple] = {}
        for metric in listed.data:
            name = metric.get("MetricName", "")
            dims = {d.get("Name"): d.get("Value")
                    for d in metric.get("Dimensions") or []}
            if name in QUEUE_METRICS and {"vhost", "queue"} <= set(dims):
                key = ("queue", dims["vhost"], dims["queue"],
                       QUEUE_METRICS[name])
            elif any(name in pair for pair in HEADROOM.values()):
                key = ("node", name)
            else:
                continue
            query_id = f"m{len(queries)}"
            where[query_id] = key
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": RABBITMQ_NAMESPACE,
                        "MetricName": name,
                        "Dimensions": metric.get("Dimensions") or [],
                    },
                    "Period": 60,
                    "Stat": "Maximum",
                },
                "ReturnData": True,
            })
        if not queries:
            listed.data = {"queues": [], "broker": {},
                           "note": f"no metrics in {RABBITMQ_NAMESPACE}"}
            return listed

        data = self.metric_data(queries[:500], minutes=minutes)
        if not data.ok:
            return data

        queues: dict[tuple, dict] = {}
        node: dict[str, float] = {}
        for query_id, (stamps, values) in data.data.items():
            key = where.get(query_id)
            if not key or not values:
                continue
            if key[0] == "queue":
                _, vhost, queue, field_name = key
                row = queues.setdefault(
                    (vhost, queue), {"vhost": vhost, "queue": queue}
                )
                row[field_name] = {
                    "now": values[-1],
                    "max": max(values),
                    "min": min(values),
                    "as_of": _iso(stamps[-1]) if stamps else "",
                }
            else:
                node[key[1]] = values[-1]

        broker = {}
        for label, (used, limit) in HEADROOM.items():
            if used in node and limit in node and node[limit]:
                broker[label] = {
                    "used": node[used],
                    "limit": node[limit],
                    "ratio": round(node[used] / node[limit], 4),
                }
        data.data = {
            "minutes": minutes,
            "queues": sorted(queues.values(),
                             key=lambda r: (r["vhost"], r["queue"])),
            "broker": broker,
        }
        return data


# ---------------------------------------------------------------------------
# CloudWatch Logs: the Celery worker logs
# ---------------------------------------------------------------------------

def _quote(text: str) -> str:
    """A Logs Insights string literal. Newlines dropped, quotes escaped."""
    flat = " ".join(str(text).split())[:200]
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def insights_query(text: str = "", *, limit: int = 50) -> str:
    """The Insights query for "lines containing `text`", or for error lines
    when `text` is empty. Newest first."""
    match = f"@message like {_quote(text)}" if text.strip() else (
        f"@message like {ERROR_PATTERN}"
    )
    limit = max(1, min(int(limit), MAX_LOG_ROWS))
    return (
        "fields @timestamp, @logStream, @log, @message"
        f" | filter {match}"
        " | sort @timestamp desc"
        f" | limit {limit}"
    )


@dataclass
class CloudWatchLogs(_AwsJson):
    system: str = "cloudwatch_logs"
    service: str = "logs"
    target_prefix: str = "Logs_20140328"
    json_version: str = "1.1"

    def log_groups(self, prefix: str) -> IntegrationResult:
        """Log group names starting with `prefix`."""
        result = self._pages(
            "DescribeLogGroups",
            {"logGroupNamePrefix": prefix, "limit": 50},
            "logGroups", token_key="nextToken",
        )
        if result.ok:
            result.data = [g.get("logGroupName", "")
                           for g in result.data["logGroups"]]
        return result

    def groups_for(self, app: str) -> IntegrationResult:
        """Every log group for an app: core, db, rabbitmq, or all."""
        if app != "all" and app not in LOG_GROUP_PREFIXES:
            return IntegrationResult(
                self.system, "groups_for", False,
                error=f"unknown app {app!r}: use core, db, rabbitmq or all",
            )
        apps = list(LOG_GROUP_PREFIXES) if app == "all" else [app]
        names: list[str] = []
        result = IntegrationResult(self.system, "groups_for", True)
        for name in apps:
            for prefix in LOG_GROUP_PREFIXES[name]:
                result = self.log_groups(prefix)
                if not result.ok:
                    return result
                names.extend(result.data)
        result.data = names
        return result

    def query(
        self,
        log_groups: list[str],
        query: str,
        *,
        minutes: int = 60,
        wait_seconds: int = 25,
    ) -> IntegrationResult:
        """Run a Logs Insights query and wait for it, up to `wait_seconds`.

        A query still running at the deadline is stopped, and whatever it
        had found is returned with status "Timeout (partial)".
        """
        if not log_groups:
            return IntegrationResult(
                self.system, "StartQuery", False,
                error="no log groups to search",
            )
        end = int(time.time())
        started = self._op("StartQuery", {
            "logGroupNames": log_groups[:MAX_LOG_GROUPS],
            "startTime": end - minutes * 60,
            "endTime": end,
            "queryString": query,
        })
        if not started.ok:
            return started
        query_id = (started.data or {}).get("queryId", "")
        if not query_id:
            return IntegrationResult(
                self.system, "StartQuery", False, error="no queryId returned"
            )

        deadline = time.monotonic() + wait_seconds
        result = started
        status = "Running"
        while True:
            result = self._op("GetQueryResults", {"queryId": query_id})
            if not result.ok:
                return result
            status = (result.data or {}).get("status", "")
            if status not in ("Running", "Scheduled"):
                break
            if time.monotonic() >= deadline:
                self._op("StopQuery", {"queryId": query_id})
                status = "Timeout (partial)"
                break
            time.sleep(1)

        data = result.data or {}
        rows = [
            {cell.get("field", ""): cell.get("value", "")
             for cell in row if cell.get("field") != "@ptr"}
            for row in data.get("results") or []
        ]
        stats = data.get("statistics") or {}
        result.data = {
            "status": status,
            "rows": rows,
            "matched": stats.get("recordsMatched"),
            "scanned_bytes": stats.get("bytesScanned"),
            "log_groups": len(log_groups[:MAX_LOG_GROUPS]),
            "minutes": minutes,
        }
        return result

    def search(
        self, text: str = "", *, app: str = "all", minutes: int = 60,
        limit: int = 50,
    ) -> IntegrationResult:
        """Worker log lines containing `text` (errors when empty)."""
        groups = self.groups_for(app)
        if not groups.ok:
            return groups
        return self.query(
            groups.data, insights_query(text, limit=limit), minutes=minutes,
        )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def describe(*, live: bool = True) -> dict:
    """Whether the keys are set and, with `live`, whether each permission
    works. Never includes a key or any part of one."""
    out: dict = {"region": region()}
    cloudwatch = CloudWatch()
    if not cloudwatch.configured():
        out["status"] = "not set (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)"
        return out
    out["status"] = "configured"
    if not live:
        return out

    checks = {
        "alarms": cloudwatch._op(
            "DescribeAlarms", {"MaxRecords": 1}
        ),
        "metrics": cloudwatch._op(
            "ListMetrics", {"Namespace": RABBITMQ_NAMESPACE}
        ),
        "worker_logs": CloudWatchLogs()._op(
            "DescribeLogGroups",
            {"logGroupNamePrefix": LOG_GROUP_PREFIXES["core"][0], "limit": 1},
        ),
    }
    for name, result in checks.items():
        out[name] = "ok" if result.ok else f"failing: {result.error[:160]}"
    return out
