"""
test_dependency_watch.py

The watch is only useful if #eng-infra keeps reading it, so the cases that
matter are the ones that would make it lie or make it noisy: calling a repo
clean when its lockfile was never read, marking an advisory fixed because a
file failed to load, saying the same thing every morning, flooding the
channel on the first run, or alerting the same day on a high advisory deep
in a build tool.
"""

from datetime import date

import httpx
import pytest

import dependency_watch as dw
from mcp_connector.repos import Repo


def _repo(slug, host="bitbucket", ref="master"):
    return Repo(slug, host, "vomeadmin" if host == "github" else "vomedjango",
                ref, "", "")


CORE = _repo("vomedjango-restored-core-app")
DB = _repo("vomedjango-database-app")
WEB = _repo("vome-react", host="github")


# ---------------------------------------------------------------------------
# Parsing the inventory
# ---------------------------------------------------------------------------

def test_requirements_pins_includes_and_ranges():
    pins, includes, unpinned = dw.parse_requirements(
        "# comment\n"
        "-r base.txt\n"
        "Django==4.2.20\n"
        "celery[redis]==5.6.3 ; python_version >= '3.9'\n"
        "Pillow == 10.4.0  # images\n"
        "requests>=2\n"
        "-e git+https://example.com/x.git#egg=x\n"
    )
    assert pins == [("Django", "4.2.20"), ("celery", "5.6.3"),
                    ("Pillow", "10.4.0")]
    assert includes == ["base.txt"]
    assert unpinned == 1


def test_package_lock_v3_keeps_dev_and_nested_copies():
    lock = {"lockfileVersion": 3, "packages": {
        "": {"name": "vome-react"},
        "node_modules/react": {"version": "18.2.0"},
        "node_modules/jest": {"version": "29.0.0", "dev": True},
        "node_modules/a/node_modules/@scope/b": {"version": "1.0.0"},
        "node_modules/linked": {"link": True, "resolved": "../x"},
    }}
    assert sorted(dw.parse_package_lock(lock)) == [
        ("@scope/b", "1.0.0", False), ("jest", "29.0.0", True),
        ("react", "18.2.0", False),
    ]


def test_package_lock_v1_walks_nested_dependencies():
    lock = {"lockfileVersion": 1, "dependencies": {
        "a": {"version": "1.0.0", "dependencies": {
            "b": {"version": "2.0.0", "dev": True}}},
    }}
    assert dw.parse_package_lock(lock) == [("a", "1.0.0", False),
                                           ("b", "2.0.0", True)]


def test_yarn_v1_and_berry():
    v1 = (
        '# yarn lockfile v1\n\n'
        '"@babel/code-frame@^7.0.0", "@babel/code-frame@^7.20.0":\n'
        '  version "7.29.0"\n'
        '  dependencies:\n'
        '    js-tokens "^4.0.0"\n\n'
        'lodash@^4.17.21:\n'
        '  version "4.17.21"\n'
    )
    assert dw.parse_yarn_lock(v1) == [("@babel/code-frame", "7.29.0", False),
                                      ("lodash", "4.17.21", False)]
    berry = (
        '__metadata:\n  version: 6\n\n'
        '"axios@npm:^1.6.0":\n  version: 1.6.8\n'
    )
    assert dw.parse_yarn_lock(berry) == [("axios", "1.6.8", False)]


# ---------------------------------------------------------------------------
# Reading the repos: unread is never clean
# ---------------------------------------------------------------------------

def _fake_reads(monkeypatch, files):
    def read(repo, path):
        value = files.get((repo.slug, path), "404")
        if isinstance(value, Exception):
            return None, str(value)
        if value == "404":
            return None, "404"
        return value, ""

    monkeypatch.setattr(dw, "_read", read)


def test_both_requirements_files_and_includes_are_read(monkeypatch):
    _fake_reads(monkeypatch, {
        (CORE.slug, "requirements.txt"): "Django==4.2.20\nPillow==10.4.0\n",
        (CORE.slug, "requirements_prod.txt"): "Pillow==10.4.0\nPyJWT==2.9.0\n",
        (DB.slug, "requirements/production.txt"): "-r base.txt\nboto3==1.3\n",
        (DB.slug, "requirements/base.txt"): "Django==4.2.7\n",
    })
    inv = dw.build_inventory([CORE, DB])
    got = sorted((p.repo, p.name, p.version) for p in inv.packages)
    assert got == [
        (DB.slug, "Django", "4.2.7"), (DB.slug, "boto3", "1.3"),
        (CORE.slug, "Django", "4.2.20"), (CORE.slug, "Pillow", "10.4.0"),
        (CORE.slug, "PyJWT", "2.9.0"),
    ]
    assert inv.scanned == {CORE.slug, DB.slug}
    assert inv.problems == []


def test_a_failed_read_leaves_the_repo_unscanned(monkeypatch):
    """One file read, one refused: the repo was only partly seen, so none of
    its old findings may be marked resolved, and the digest must say so."""
    _fake_reads(monkeypatch, {
        (CORE.slug, "requirements.txt"): "Django==4.2.20\n",
        (CORE.slug, "requirements_prod.txt"): RuntimeError("HTTP 401"),
    })
    inv = dw.build_inventory([CORE])
    assert CORE.slug not in inv.scanned
    assert any("core" in p and "401" in p for p in inv.problems)


def test_a_repo_with_no_lockfile_is_reported_not_skipped(monkeypatch):
    _fake_reads(monkeypatch, {})
    inv = dw.build_inventory([WEB, CORE])
    assert inv.scanned == set()
    assert any(p.startswith("web:") for p in inv.problems)
    assert any(p.startswith("core:") for p in inv.problems)


def test_npm_dev_dependencies_and_node_version(monkeypatch):
    _fake_reads(monkeypatch, {
        (WEB.slug, "package.json"): {"devDependencies": {"jest": "^29"}},
        (WEB.slug, "package-lock.json"): {"packages": {
            "node_modules/react": {"version": "18.2.0"},
            "node_modules/jest": {"version": "29.0.0"},
        }},
        (WEB.slug, dw.NODE_WORKFLOW): 'env:\n  NODE_VERSION: "16.12.0"\n',
    })
    inv = dw.build_inventory([WEB])
    dev = {p.name: p.dev for p in inv.packages}
    assert dev == {"react": False, "jest": True}
    assert inv.node == {WEB.slug: "16.12.0"}


# ---------------------------------------------------------------------------
# Severity and fixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5),
    ("CVSS:3.0/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N", 0.0),
])
def test_cvss3_base_score(vector, score):
    assert dw.cvss3_score(vector) == score


def test_severity_prefers_the_label_then_the_vector():
    assert dw.severity_of({"database_specific": {"severity": "MODERATE"}}) \
        == "MEDIUM"
    assert dw.severity_of({"severity": [{
        "type": "CVSS_V3",
        "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}) \
        == "CRITICAL"
    assert dw.severity_of({"id": "MAL-2026-1"}) == "CRITICAL"
    assert dw.severity_of({"severity": [{"score": "CVSS:4.0/AV:N"}]}) \
        == "UNKNOWN"


def test_advisory_key_is_the_cve_when_there_is_one():
    assert dw.advisory_key({"id": "PYSEC-1",
                            "aliases": ["GHSA-x", "CVE-2026-2"]}) \
        == "CVE-2026-2"
    assert dw.advisory_key({"id": "GHSA-y", "aliases": []}) == "GHSA-y"


def _record(vid, fixed, name="pillow", eco="PyPI", aliases=(), sev="HIGH"):
    events = [{"introduced": "0"}] + ([{"fixed": f} for f in fixed])
    return {"id": vid, "aliases": list(aliases),
            "summary": f"{vid} summary",
            "database_specific": {"severity": sev} if sev else {},
            "affected": [{"package": {"ecosystem": eco, "name": name},
                          "ranges": [{"type": "ECOSYSTEM",
                                      "events": events}]}]}


def test_fixed_in_is_the_nearest_release_that_fixes_everything():
    records = [_record("A", ["9.0.0", "10.4.1"]), _record("B", ["13.0.0"])]
    assert dw.fixed_in(records, "PyPI", "Pillow", "10.4.0") == "13.0.0"
    assert dw.fixed_in([_record("A", ["10.4.1", "11.0.0"])], "PyPI",
                       "Pillow", "10.4.0") == "10.4.1"
    assert dw.fixed_in([_record("A", ["13.0.0"]), _record("B", [])],
                       "PyPI", "Pillow", "10.4.0") == ""


# ---------------------------------------------------------------------------
# Matching against OSV, KEV and EPSS
# ---------------------------------------------------------------------------

def _fake_feeds(monkeypatch, records, hits, kev=None, epss=None):
    """OSV, KEV and EPSS answered from memory."""
    def post_json(url, body):
        results = []
        for q in body["queries"]:
            ids = hits.get((q["package"]["name"], q["version"]), [])
            results.append({"vulns": [{"id": i} for i in ids]})
        return {"results": results}

    def get_json(url, params=None):
        if "/vulns/" in url:
            return records[url.rsplit("/", 1)[-1]]
        if url == dw.KEV_URL:
            if kev is None:
                raise RuntimeError("CISA down")
            return {"vulnerabilities": [{"cveID": c, "dateAdded": d}
                                        for c, d in kev.items()]}
        if url == dw.EPSS_URL:
            return {"data": [{"cve": c, "epss": str(v)}
                             for c, v in (epss or {}).items()]}
        raise AssertionError(url)

    monkeypatch.setattr(dw, "_post_json", post_json)
    monkeypatch.setattr(dw, "_get_json", get_json)


def test_duplicate_records_collapse_and_get_enriched(monkeypatch):
    records = {
        "GHSA-p": _record("GHSA-p", ["13.0.0"], aliases=["CVE-2026-112900"],
                          sev="CRITICAL"),
        "PYSEC-p": _record("PYSEC-p", ["13.0.0"],
                           aliases=["CVE-2026-112900"], sev=""),
    }
    _fake_feeds(monkeypatch, records,
                {("Pillow", "10.4.0"): ["GHSA-p", "PYSEC-p"]},
                kev={"CVE-2026-112900": "2026-10-20"},
                epss={"CVE-2026-112900": 0.42})
    inv = dw.Inventory(packages=[
        dw.Package("PyPI", "Pillow", "10.4.0", CORE.slug)])
    findings, problems = dw.match(inv)
    assert problems == []
    assert len(findings) == 1
    f = findings[0]
    assert (f.key, f.severity, f.fixed, f.kev, f.epss) == (
        "CVE-2026-112900", "CRITICAL", "13.0.0", True, 0.42)
    assert f.link.endswith("GHSA-p")


def test_a_silent_kev_feed_is_named_not_assumed_clean(monkeypatch):
    records = {"GHSA-p": _record("GHSA-p", ["13.0.0"], aliases=["CVE-1"])}
    _fake_feeds(monkeypatch, records, {("Pillow", "10.4.0"): ["GHSA-p"]},
                kev=None)
    inv = dw.Inventory(packages=[
        dw.Package("PyPI", "Pillow", "10.4.0", CORE.slug)])
    findings, problems = dw.match(inv)
    assert len(findings) == 1 and not findings[0].kev
    assert any("KEV" in p for p in problems)


def test_nvd_only_fills_a_missing_severity(monkeypatch):
    records = {
        "GHSA-a": _record("GHSA-a", ["2.0"], aliases=["CVE-A"], sev=""),
        "GHSA-b": _record("GHSA-b", ["2.0"], aliases=["CVE-B"], sev="LOW"),
    }
    _fake_feeds(monkeypatch, records,
                {("Pillow", "1.0"): ["GHSA-a", "GHSA-b"]}, kev={})
    asked = []

    def nvd(cves):
        asked.extend(cves)
        return {c: "CRITICAL" for c in cves}, 0

    monkeypatch.setattr(dw, "nvd_severity", nvd)
    inv = dw.Inventory(packages=[dw.Package("PyPI", "Pillow", "1.0",
                                            CORE.slug)])
    monkeypatch.delenv("NVD_API_KEY", raising=False)
    findings, _ = dw.match(inv)
    assert asked == []
    assert {f.key: f.severity for f in findings}["CVE-A"] == "UNKNOWN"

    monkeypatch.setenv("NVD_API_KEY", "k")
    findings, problems = dw.match(inv)
    # Only the CVE OSV left blank is looked up, and OSV's LOW stands.
    assert asked == ["CVE-A"]
    assert {f.key: f.severity for f in findings} == {
        "CVE-A": "CRITICAL", "CVE-B": "LOW"}
    assert problems == []


def test_nvd_prefers_the_primary_score_and_counts_failures(monkeypatch):
    def get(url, params=None, headers=None, timeout=None):
        assert headers == {"apiKey": "k"}
        cve = params["cveId"]
        if cve == "CVE-DOWN":
            raise RuntimeError("503")
        metrics = {"cvssMetricV31": [
            {"type": "Secondary", "cvssData": {"baseSeverity": "LOW"}},
            {"type": "Primary", "cvssData": {"baseSeverity": "HIGH"}},
        ]} if cve == "CVE-1" else {}
        resp = httpx.Response(200, json={"vulnerabilities": [
            {"cve": {"id": cve, "metrics": metrics}}]},
            request=httpx.Request("GET", url))
        return resp

    monkeypatch.setenv("NVD_API_KEY", "k")
    monkeypatch.setattr(dw.httpx, "get", get)
    monkeypatch.setattr(dw.time, "sleep", lambda s: None)
    found, failed = dw.nvd_severity(["CVE-1", "CVE-NONE", "CVE-DOWN"])
    assert found == {"CVE-1": "HIGH"}
    assert failed == 1


def test_osv_down_matches_nothing_and_says_so(monkeypatch):
    def boom(url, body):
        raise RuntimeError("503")

    monkeypatch.setattr(dw, "_post_json", boom)
    inv = dw.Inventory(packages=[
        dw.Package("PyPI", "Pillow", "10.4.0", CORE.slug)])
    findings, problems = dw.match(inv)
    assert findings == []
    assert "OSV.dev did not answer" in problems[0]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _finding(package="Pillow", severity="HIGH", fixed="13.0.0", eco="PyPI",
             kev=False, epss=None, dev=False, key="CVE-1", repo=CORE.slug,
             version="10.4.0"):
    return dw.Finding(key=key, ids=["GHSA-1"], ecosystem=eco,
                      package=package, version=version, repo=repo,
                      severity=severity, summary="s", fixed=fixed, dev=dev,
                      kev=kev, epss=epss)


@pytest.mark.parametrize("finding,expected", [
    (_finding(kev=True, dev=True, fixed=""), dw.KEV),
    (_finding(severity="CRITICAL", dev=True), dw.DIGEST),
    (_finding(severity="CRITICAL", package="loader-utils", eco="npm"),
     dw.URGENT),
    (_finding(severity="CRITICAL", fixed=""), dw.DIGEST),
    (_finding(severity="HIGH"), dw.URGENT),
    (_finding(severity="HIGH", package="shell-quote", eco="npm"), dw.DIGEST),
    (_finding(severity="MEDIUM", epss=0.5, fixed=""), dw.URGENT),
    (_finding(severity="MEDIUM"), dw.DIGEST),
])
def test_tiers(monkeypatch, finding, expected):
    monkeypatch.delenv("DEPENDENCY_WATCH_EPSS", raising=False)
    assert dw.tier(finding) == expected


def test_extra_heavily_used_packages_from_env(monkeypatch):
    monkeypatch.setenv("DEPENDENCY_WATCH_PACKAGES", "npm:Shell-Quote")
    assert dw.tier(_finding(package="shell-quote", eco="npm")) == dw.URGENT


def test_each_thing_is_said_once_unless_it_gets_worse():
    row = dw._row(_finding(severity="HIGH"), dw.WATCHLIST)
    assert dw.needs_alert(row, None)
    told = {"alerted_at": "x", "alerted_severity": "HIGH",
            "alerted_kev": False}
    assert not dw.needs_alert(row, told)
    worse = dw._row(_finding(severity="CRITICAL"), dw.WATCHLIST)
    assert dw.needs_alert(worse, told)
    exploited = dw._row(_finding(severity="HIGH", kev=True), dw.WATCHLIST)
    assert dw.needs_alert(exploited, told)
    quieter = dw._row(_finding(severity="MEDIUM", epss=0.9), dw.WATCHLIST)
    assert not dw.needs_alert(quieter, {**told, "alerted_severity": "HIGH"})
    digest_only = dw._row(_finding(package="x", severity="MEDIUM"),
                          dw.WATCHLIST)
    assert not dw.needs_alert(digest_only, None)


# ---------------------------------------------------------------------------
# End of life
# ---------------------------------------------------------------------------

def test_end_of_life(monkeypatch):
    cycles = {
        "django": [{"cycle": "5.2", "eol": "2028-04-30"},
                   {"cycle": "4.2", "eol": "2026-04-30", "latest": "4.2.24"}],
        "nodejs": [{"cycle": "16", "eol": "2023-09-11"}],
        "react": [{"cycle": "18", "eol": False}],
        "python": [{"cycle": "3.10", "eol": "2026-12-31"}],
        "postgresql": [{"cycle": "14", "eol": "2026-11-12"}],
    }
    monkeypatch.setattr(dw, "_get_json",
                        lambda url, params=None: cycles[url.split("/")[-1]
                                                        .split(".")[0]])
    monkeypatch.setenv("DEPENDENCY_WATCH_RUNTIMES",
                       "python=3.10.12,postgresql=14,redis=6.0")
    cycles["redis"] = [{"cycle": "7.2", "eol": False}]
    inv = dw.Inventory(
        packages=[dw.Package("PyPI", "Django", "4.2.20", CORE.slug),
                  dw.Package("PyPI", "Django", "4.2.7", DB.slug),
                  dw.Package("npm", "react", "18.2.0", WEB.slug)],
        node={WEB.slug: "16.12.0"},
    )
    eols, problems = dw.check_eol(inv, today=date(2026, 10, 24))
    by = {(e.product, e.version): e for e in eols}
    assert by[("django", "4.2.20")].status == "eol"
    assert by[("django", "4.2.20")].eol_date == "2026-04-30"
    assert by[("django", "4.2.20")].upgrade_to == "5.2"
    # Two patch releases of one cycle: one line, every repo on it.
    assert [e.where for e in eols if e.product == "django"] == [
        ["core", "database"]]
    assert by[("nodejs", "16.12.0")].status == "eol"
    assert by[("nodejs", "16.12.0")].where == ["web build"]
    assert by[("react", "18.2.0")].status == "ok"
    assert by[("python", "3.10.12")].status == "soon"
    assert by[("redis", "6.0")].status == "unlisted"
    assert problems == []


def test_unknown_runtimes_are_named(monkeypatch):
    monkeypatch.delenv("DEPENDENCY_WATCH_RUNTIMES", raising=False)
    eols, problems = dw.check_eol(dw.Inventory(), today=date(2026, 10, 24))
    assert eols == []
    assert "Python, PostgreSQL, Redis: version unknown" in problems[0]


# ---------------------------------------------------------------------------
# The runs
# ---------------------------------------------------------------------------

class FakeLedger:
    """The ledger's contract, in memory."""
    rows: dict = {}
    baseline = False

    def __init__(self, enabled=True):
        self.available = enabled

    def told(self):
        return dict(FakeLedger.rows)

    def baseline_done(self):
        return FakeLedger.baseline or not self.available

    def mark_baseline(self):
        FakeLedger.baseline = True

    def record(self, rows):
        for r in rows:
            FakeLedger.rows.setdefault(r.ident, {})

    def resolve_missing(self, seen, scanned):
        return 0

    def resolved_since(self, since):
        return 0

    def set_task(self, rows, task_id):
        for r in rows:
            FakeLedger.rows[r.ident]["clickup_task_id"] = task_id

    def clear_task(self, task_id):
        for row in FakeLedger.rows.values():
            if row.get("clickup_task_id") == task_id:
                row["clickup_task_id"] = None

    def plan(self):
        return {(k[1], k[2]): dict(v) for k, v in FakeLedger.rows.items()
                if k[0] == "PLAN"}

    def set_plan(self, owner, slot, task_id, fingerprint):
        FakeLedger.rows[("PLAN", owner, slot)] = {
            "clickup_task_id": task_id, "summary": fingerprint}

    def drop_plan(self, owner, slot):
        FakeLedger.rows.pop(("PLAN", owner, slot), None)

    def mark_alerted(self, rows):
        for r in rows:
            FakeLedger.rows[r.ident].update(
                alerted_at="now", alerted_severity=r.severity,
                alerted_kev=r.kev)


class FakeClickUp:
    """ClickUp's task, comment and checklist endpoints, in memory."""

    def __init__(self):
        self.tasks, self.comments, self.checklists = {}, [], {}

    def __call__(self, method, path, body=None):
        parts = path.strip("/").split("/")
        if method == "POST" and parts[0] == "list":
            tid = f"t{len(self.tasks) + 1}"
            self.tasks[tid] = {**body, "id": tid, "status": "queued"}
            return {"id": tid}
        if parts[0] == "task" and parts[-1] == "comment":
            self.comments.append((parts[1], body["comment_text"]))
            return {}
        if parts[0] == "task" and parts[-1] == "checklist":
            checklist = {"id": f"c{parts[1]}", "name": body["name"],
                         "items": []}
            self.checklists[parts[1]] = checklist
            return {"checklist": checklist}
        if method == "GET" and parts[0] == "task":
            checklist = self.checklists.get(parts[1])
            return {"checklists": [checklist] if checklist else []}
        if method == "PUT" and parts[0] == "task":
            self.tasks.setdefault(parts[1], {"id": parts[1]}).update(body)
            return {}
        if parts[0] == "checklist":
            checklist = next(c for c in self.checklists.values()
                             if c["id"] == parts[1])
            if method == "POST":
                checklist["items"].append({
                    "id": f"i{len(checklist['items'])}",
                    "name": body["name"], "resolved": False})
            else:
                next(i for i in checklist["items"]
                     if i["id"] == parts[3]).update(body)
            return {}
        raise AssertionError(f"unexpected ClickUp call {method} {path}")

    def named(self):
        return sorted(t["name"] for t in self.tasks.values() if "name" in t)

    def subtasks(self, parent):
        return [t for t in self.tasks.values() if t.get("parent") == parent]

    def items(self, task_id):
        return [(i["name"], i["resolved"])
                for i in self.checklists[task_id]["items"]]


@pytest.fixture
def world(monkeypatch):
    FakeLedger.rows, FakeLedger.baseline = {}, False
    posts = []
    clickup = FakeClickUp()
    state = {"findings": [_finding(severity="HIGH")], "replies": [],
             "scanned": {CORE.slug}, "eols": []}

    def fake_scan(repos=None):
        inv = dw.Inventory(packages=[dw.Package("PyPI", "Pillow", "10.4.0",
                                                CORE.slug)],
                           scanned=set(state["scanned"]))
        rows = [dw._row(f, dw.WATCHLIST) for f in state["findings"]] + \
            dw._eol_rows(state["eols"])
        return dw.Scan(inv, state["findings"], state["eols"], [], rows,
                       {f.key: f.link for f in state["findings"]})

    def fake_post(text, thread_ts=""):
        # `posts` is what lands in the channel; thread replies kept apart.
        if thread_ts:
            state["replies"].append((thread_ts, text))
            return f"{thread_ts}.r"
        posts.append(text)
        return f"ts{len(posts)}"

    monkeypatch.setattr(dw, "Ledger", FakeLedger)
    monkeypatch.setattr(dw, "scan", fake_scan)
    monkeypatch.setattr(dw, "_post", fake_post)
    monkeypatch.setattr(dw, "_clickup", clickup)
    monkeypatch.setattr(dw, "_clickup_user",
                        lambda owner: {"onlyg": 49257687,
                                       "sanjay": 4434086}[owner])
    monkeypatch.setattr(dw, "_ask_claude", lambda prompt: "")
    monkeypatch.setattr(dw, "_mention", lambda name: f"@{name}")
    monkeypatch.setenv("DEPENDENCY_WATCH_ENABLED", "true")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "t")
    monkeypatch.delenv("DEPENDENCY_WATCH_EPSS", raising=False)
    return state, posts, clickup


def test_first_run_is_one_baseline_post_then_silence(world):
    state, posts, clickup = world
    result = dw.run_daily_scan()
    assert result["status"] == "baseline"
    assert len(posts) == 1 and "first scan" in posts[0]
    assert dw.run_daily_scan()["status"] == "ok"
    assert len(posts) == 1


def test_a_new_finding_alerts_once(world):
    state, posts, clickup = world
    dw.run_daily_scan()
    state["findings"] = state["findings"] + [
        _finding(package="Django", severity="CRITICAL", key="CVE-9",
                 version="4.2.20", fixed="4.2.25")]
    dw.run_daily_scan()
    assert len(posts) == 2
    assert "*Django* 4.2.20 in core" in posts[1]
    assert "Pillow" not in posts[1]
    dw.run_daily_scan()
    assert len(posts) == 2


def test_kev_gets_one_subtask_even_when_slack_fails(world, monkeypatch):
    state, posts, clickup = world
    dw.run_daily_scan()
    state["findings"] = [_finding(severity="HIGH", kev=True)]
    monkeypatch.setattr(dw, "_post", lambda text, thread_ts="": "")
    dw.run_daily_scan()
    dw.run_daily_scan()
    # The parent and one Pillow subtask, never a second.
    assert len(clickup.tasks) == 2


def test_a_failed_baseline_post_is_retried_as_a_baseline(world, monkeypatch):
    state, posts, clickup = world
    monkeypatch.setattr(dw, "_post", lambda text, thread_ts="": "")
    assert dw.run_daily_scan()["posted"] is False
    monkeypatch.setattr(dw, "_post",
                        lambda text, thread_ts="":
                        thread_ts or posts.append(text) or "ts")
    assert dw.run_daily_scan()["status"] == "baseline"
    assert len(posts) == 1


def test_disabled_does_nothing(world, monkeypatch):
    state, posts, clickup = world
    monkeypatch.setenv("DEPENDENCY_WATCH_ENABLED", "false")
    assert dw.run_daily_scan() == {"status": "disabled"}
    assert dw.run_weekly_digest() == {"status": "disabled"}
    assert posts == []


def test_claude_notes_the_alert_in_its_thread(world, monkeypatch):
    state, posts, clickup = world
    dw.run_daily_scan()
    sent = []
    monkeypatch.setattr(dw, "_post", lambda text, thread_ts="":
                        sent.append((text, thread_ts)) or "ts9")
    prompts = []
    monkeypatch.setattr(dw, "_ask_claude", lambda prompt: prompts.append(
        prompt) or "• *Django*: a crafted URL \N{EM DASH} upgrade first.")
    finding = _finding(package="Django", severity="CRITICAL", key="CVE-9",
                       version="4.2.20", fixed="4.2.25")
    finding.details = "Reached through the URL resolver."
    state["findings"] = state["findings"] + [finding]
    monkeypatch.setattr(dw, "scan", _scan_with_details(state))
    dw.run_daily_scan()
    assert sent[0][1] == "" and "*Django*" in sent[0][0]
    assert sent[1][1] == "ts9"
    assert sent[1][0].startswith("*Claude's read*")
    assert "\N{EM DASH}" not in sent[1][0]
    # Claude sees only what was flagged, with the advisory text.
    assert "Reached through the URL resolver." in prompts[0]
    assert "Pillow" not in prompts[0]


def test_the_alert_posts_when_claude_is_silent(world, monkeypatch):
    state, posts, clickup = world
    dw.run_daily_scan()
    state["findings"] = state["findings"] + [
        _finding(package="Django", severity="CRITICAL", key="CVE-9",
                 version="4.2.20", fixed="4.2.25")]
    dw.run_daily_scan()
    assert len(posts) == 2 and "*Django*" in posts[1]


def _scan_with_details(state):
    def fake_scan(repos=None):
        inv = dw.Inventory(packages=[dw.Package("PyPI", "Pillow", "10.4.0",
                                                CORE.slug)],
                           scanned={CORE.slug})
        rows = [dw._row(f, dw.WATCHLIST) for f in state["findings"]]
        return dw.Scan(inv, state["findings"], [], [], rows,
                       {f.key: f.link for f in state["findings"]},
                       {f.key: f.details for f in state["findings"]
                        if f.details})
    return fake_scan


# ---------------------------------------------------------------------------
# Who owns it: backend is OnlyG's, frontend and mobile are Sanjay's
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("repo,owner", [
    (CORE.slug, "onlyg"), (DB.slug, "onlyg"),
    ("vomedjango-chats-app", "onlyg"), ("vomedjango-integrations-app",
                                        "onlyg"),
    ("servers", "onlyg"), ("core", "onlyg"),
    (WEB.slug, "sanjay"), ("vome-landing-page", "sanjay"),
    ("VomeApp", "sanjay"), ("web build", "sanjay"),
])
def test_owner_of(repo, owner):
    assert dw.owner_of(repo) == owner


def test_the_alert_tags_the_owners_and_copies_sam(world):
    state, posts, clickup = world
    dw.run_daily_scan()
    state["findings"] = state["findings"] + [
        _finding(package="Django", severity="CRITICAL", key="CVE-9",
                 version="4.2.20", fixed="4.2.25"),
        _finding(package="handlebars", eco="npm", severity="CRITICAL",
                 key="CVE-7", repo=WEB.slug, version="4.7.9",
                 fixed="4.7.10"),
    ]
    dw.run_daily_scan()
    alert = posts[-1]
    assert alert.startswith("@onlyg @sanjay cc @sam :rotating_light:")
    # Each line links the subtask that holds it, and nothing else posts.
    assert "Owner: <https://app.clickup.com/t/" in alert
    assert "|OnlyG's task>" in alert and "|Sanjay's task>" in alert
    assert len(posts) == 2


def test_one_owner_tags_only_that_owner(world):
    state, posts, clickup = world
    dw.run_daily_scan()
    state["findings"] = state["findings"] + [
        _finding(package="handlebars", eco="npm", severity="CRITICAL",
                 key="CVE-7", repo=WEB.slug, version="4.7.9",
                 fixed="4.7.10")]
    dw.run_daily_scan()
    assert posts[-1].startswith("@sanjay cc @sam ")
    assert "@onlyg" not in posts[-1]


# ---------------------------------------------------------------------------
# The upgrade plan: one task per engineer, one subtask per upgrade
# ---------------------------------------------------------------------------

DJANGO_EOL = dw.Eol("django", "4.2.20", "4.2", "eol", "2026-04-07", "4.2.24",
                    ["core"], "5.2 LTS")


def test_one_task_per_engineer_with_a_subtask_per_upgrade(world):
    """Sam, 2026-10-26: 25 separate tasks in one morning was too many. Each
    engineer gets one task, each upgrade a subtask saying how urgent it is,
    what version to what, and how big the job is."""
    state, posts, clickup = world
    state["findings"] = [
        _finding(severity="HIGH", kev=True, key="CVE-2023-4863",
                 fixed="10.0.1", version="10.0.0",
                 repo="vomedjango-integrations-app"),
        _finding(severity="CRITICAL", key="CVE-2026-112393", fixed="13.0.0"),
        _finding(package="Django", severity="CRITICAL", key="CVE-9",
                 version="4.2.20", fixed="5.2.17"),
        _finding(package="anyio", severity="CRITICAL", key="CVE-5",
                 version="4.14.1", fixed="4.14.2"),
        _finding(package="handlebars", eco="npm", severity="CRITICAL",
                 key="CVE-7", repo=WEB.slug, version="4.7.9",
                 fixed="4.7.10"),
        _finding(package="x", severity="MEDIUM", key="CVE-8"),  # digest
    ]
    state["eols"] = [DJANGO_EOL]
    dw.run_daily_scan()
    assert clickup.named() == [
        "Critical (exploited) · Pillow 10.0.0, 10.4.0 → 13.0.0 · Major "
        "upgrade",
        "High · Django 4.2.20 → 5.2.17 · Project",
        "High · Quick upgrades · 1 package",
        "High · Quick upgrades · 1 package",
        "[Security] Backend dependency upgrades",
        "[Security] Frontend dependency upgrades",
    ]
    backend = clickup.tasks["t1"]
    assert backend["name"] == "[Security] Backend dependency upgrades"
    assert backend["assignees"] == [49257687]
    assert len(clickup.subtasks("t1")) == 3
    assert [t["name"] for t in clickup.subtasks("t5")] == [
        "High · Quick upgrades · 1 package"]
    pillow = next(t for t in clickup.subtasks("t1")
                  if "Pillow" in t["name"])
    assert pillow["assignees"] == [49257687] and pillow["priority"] == 1
    assert "CVE-2023-4863 on 10.0.0 in integrations" in \
        pillow["markdown_description"]
    assert clickup.items("t4") == [("anyio 4.14.1 → 4.14.2 (core) · Patch",
                                    False)]
    # The parent lists its subtasks by section and takes the top priority.
    assert backend["priority"] == 1
    assert "**Critical** (exploited in the wild" in \
        backend["markdown_content"]
    assert "- [High · Django 4.2.20 → 5.2.17 · Project](" \
           "https://app.clickup.com/t/t2)" in backend["markdown_content"]

    assert len(posts) == 1
    assert ("*Assigned*: <https://app.clickup.com/t/t1|Backend dependency "
            "upgrades> for @onlyg, <https://app.clickup.com/t/t5|Frontend "
            "dependency upgrades> for @sanjay, cc @sam.") in posts[0]
    plan_list = state["replies"][0][1]
    assert ("• <https://app.clickup.com/t/t3|Critical (exploited) · Pillow "
            "10.0.0, 10.4.0 → 13.0.0 · Major upgrade>") in plan_list
    for task in clickup.tasks.values():
        text = task["name"] + task.get("markdown_description", "") + \
            task.get("markdown_content", "")
        assert "\N{EM DASH}" not in text

    # Another Pillow advisory joins the Pillow subtask, never a second one.
    state["findings"].append(_finding(severity="CRITICAL", key="CVE-NEW",
                                      fixed="13.0.0"))
    dw.run_daily_scan()
    assert len(clickup.tasks) == 6
    assert "|OnlyG's task>" in posts[-1]


def test_the_old_per_package_tasks_move_into_the_plan(world):
    """Oct 25 and 26 filed one task per package. The next run closes each
    with a link to its subtask, says so once, and never again."""
    state, posts, clickup = world
    state["findings"] = [
        _finding(severity="HIGH", kev=True, key="CVE-2023-4863"),
        _finding(package="anyio", severity="CRITICAL", key="CVE-5",
                 version="4.14.1", fixed="4.14.2"),
        _finding(package="handlebars", eco="npm", severity="CRITICAL",
                 key="CVE-7", repo=WEB.slug, version="4.7.9",
                 fixed="4.7.10"),
    ]
    FakeLedger.baseline = True
    old = {"Pillow": "868mmzwwe", "anyio": "868mn4vx1",
           "handlebars": "868mn4we2"}
    for f in state["findings"]:
        row = dw._row(f, dw.WATCHLIST)
        FakeLedger.rows[row.ident] = {
            "alerted_at": "x", "alerted_severity": row.severity,
            "alerted_kev": row.kev, "clickup_task_id": old[f.package]}
    result = dw.run_daily_scan()
    assert result["moved"] == 3
    assert {clickup.tasks[t]["status"] for t in old.values()} == {"Closed"}
    notes = dict(clickup.comments)
    assert notes["868mmzwwe"].startswith(
        "Moved: this upgrade is now the subtask \"Critical (exploited) · "
        "Pillow 10.4.0 → 13.0.0 · Major upgrade\" "
        "(https://app.clickup.com/t/")
    assert len(posts) == 1
    assert posts[0].startswith(
        "@onlyg @sanjay cc @sam :clipboard: *Dependency upgrades* are now "
        "one task per engineer")
    assert "Backend dependency upgrades> (OnlyG): 1 critical, 1 quick " \
           "upgrade on one checklist" in posts[0]
    assert "The 3 earlier tasks are closed" in posts[0]
    assert {v["clickup_task_id"] for k, v in FakeLedger.rows.items()
            if k[0] not in ("PLAN",)} <= set(clickup.tasks) - set(old.values())
    dw.run_daily_scan()
    assert len(posts) == 1 and len(clickup.comments) == 3


def test_a_subtask_closes_only_after_a_run_that_read_everything(world):
    state, posts, clickup = world
    dw.run_daily_scan()  # the Pillow subtask, t2 under t1
    state["findings"] = []
    dw.run_daily_scan()  # only core was read: unread is not fixed
    assert clickup.tasks["t2"]["status"] == "queued"
    state["scanned"] = set(dw.MANIFESTS)
    result = dw.run_daily_scan()
    assert result["closed"] == [("onlyg", "pillow")]
    assert clickup.tasks["t2"]["status"] == "Closed"
    assert clickup.tasks["t1"]["status"] == "Closed"  # nothing left on it
    assert ("PLAN", "onlyg", "*") not in FakeLedger.rows


def test_quick_upgrades_share_one_checklist_that_keeps_itself(world):
    state, posts, clickup = world
    state["scanned"] = set(dw.MANIFESTS)
    state["findings"] = [
        _finding(package="anyio", severity="CRITICAL", key="CVE-5",
                 version="4.14.1", fixed="4.14.2"),
        _finding(package="certifi", severity="CRITICAL", key="CVE-6",
                 version="2023.7.22", fixed="2026.10.19", repo=DB.slug),
    ]
    dw.run_daily_scan()
    quick = next(t for t in clickup.tasks.values()
                 if "Quick upgrades" in t["name"])
    assert quick["name"] == "High · Quick upgrades · 2 packages"
    assert clickup.items(quick["id"]) == [
        ("anyio 4.14.1 → 4.14.2 (core) · Patch", False),
        ("certifi 2023.7.22 → 2026.10.19 (database) · Minor", False)]
    # anyio ships; certifi's fix moves on.
    state["findings"] = [_finding(package="certifi", severity="CRITICAL",
                                  key="CVE-6", version="2023.7.22",
                                  fixed="2026.10.20", repo=DB.slug)]
    dw.run_daily_scan()
    assert clickup.items(quick["id"]) == [
        ("anyio 4.14.1 → 4.14.2 (core) · Patch", True),
        ("certifi 2023.7.22 → 2026.10.20 (database) · Minor", False)]
    assert quick["name"] == "High · Quick upgrades · 1 package"


@pytest.mark.parametrize("name,current,target,size", [
    ("anyio", "4.14.1", "4.14.2", "patch"),
    ("djangorestframework", "3.15.2", "3.18.2", "minor"),
    ("sqlparse", "0.4.4", "0.6.0", "minor"),
    ("eventsource", "0.1.6", "1.1.1", "major"),
    ("certifi", "2023.7.22", "2026.10.19", "minor"),
    ("Twisted", "25.5.0", "26.10.0rc1", "minor"),
    ("Pillow", "10.0.0", "13.0.0", "major"),
])
def test_how_big(name, current, target, size):
    assert dw._bump(name, current, target) == size


def test_django_and_its_end_of_life_are_one_project():
    rows = [dw._row(_finding(package="Django", severity="CRITICAL",
                             version="4.2.20", fixed="5.2.17"),
                    dw.WATCHLIST)] + dw._eol_rows([DJANGO_EOL])
    units = dw.plan_units(rows)
    assert list(units) == [("onlyg", "django")]
    rows = units[("onlyg", "django")]
    assert dw.subtask_title("django", rows) == \
        "High · Django 4.2.20 → 5.2.17 · Project"
    body = dw.subtask_body("django", rows, {})
    assert ("**How urgent:** High. Advisories in code we ship (1 critical), "
            "and Django 4.2 is out of support (supported until "
            "2026-04-07).") in body
    assert "**How big:** Project." in body


def test_node_16_is_a_project_to_the_newest_lts():
    rows = dw._eol_rows([dw.Eol("nodejs", "16.12.0", "16", "eol",
                                "2023-09-11", "16.20.2", ["web build"],
                                "24 LTS")])
    assert dw.subtask_title("nodejs", rows) == \
        "High · Node.js 16.12.0 → 24 LTS · Project"
    assert dw._slot("nodejs", rows) == "nodejs"
    assert dw.owner_of(rows[0].repo) == "sanjay"


def test_the_upgrade_cycle_is_the_newest_supported_lts():
    today = date(2026, 10, 26)
    node = [{"cycle": "26", "lts": "2026-10-28", "eol": "2029-04-30"},
            {"cycle": "25", "lts": False, "eol": "2026-06-01"},
            {"cycle": "24", "lts": "2025-10-28", "eol": "2028-04-30"},
            {"cycle": "16", "lts": "2021-10-26", "eol": "2023-09-11"}]
    assert dw._upgrade_cycle(node, today) == "24 LTS"
    django = [{"cycle": "6.0", "lts": False, "eol": "2027-04-30"},
              {"cycle": "5.2", "lts": True, "eol": "2028-04-30"},
              {"cycle": "4.2", "lts": True, "eol": "2026-04-07"}]
    assert dw._upgrade_cycle(django, today) == "5.2 LTS"
    python = [{"cycle": "3.14", "eol": "2030-10-31"},
              {"cycle": "3.10", "eol": "2026-10-31"}]
    assert dw._upgrade_cycle(python, today) == "3.14"


def test_react_pdf_and_pdfjs_are_one_upgrade():
    rows = [
        dw._row(_finding(package="pdfjs-dist", eco="npm", key="CVE-2024-4367",
                         repo=WEB.slug, version="2.12.313", fixed="4.2.67"),
                dw.WATCHLIST),
        dw._row(_finding(package="react-pdf", eco="npm",
                         key="CVE-2024-34342", repo=WEB.slug,
                         version="5.7.2", fixed="7.7.3"), dw.WATCHLIST),
    ]
    units = dw.plan_units(rows)
    assert list(units) == [("sanjay", "react-pdf")]
    assert dw.subtask_title("react-pdf", units[("sanjay", "react-pdf")]) == (
        "High · react-pdf 5.7.2 → 7.7.3, pdfjs-dist 2.12.313 → 4.2.67 · "
        "Major upgrade")


def test_a_release_candidate_target_says_so():
    rows = [dw._row(_finding(package="Twisted", key="CVE-3", version="25.5.0",
                             fixed="26.10.0rc1"), dw.WATCHLIST)]
    assert dw._move_text("twisted", rows) == \
        "Twisted 25.5.0 → 26.10.0rc1 (release candidate)"


def test_build_tools_are_low_and_wait_on_the_checklist():
    rows = [dw._row(_finding(package="virtualenv", severity="CRITICAL",
                             version="20.26.2", fixed="21.14.9",
                             repo="vomedjango-integrations-app"),
                    dw.WATCHLIST)]
    assert dw._level("virtualenv", rows) == "low"
    assert dw._slot("virtualenv", rows) == dw.QUICK


def test_the_digest_tags_nobody(world, monkeypatch):
    state, posts, clickup = world
    dw.run_daily_scan()
    monkeypatch.setattr(dw, "hn_chatter", lambda days: [])
    monkeypatch.setattr(dw, "_digest_week_due", lambda today: True)
    dw.run_weekly_digest()
    assert "@" not in posts[-1]


def test_every_other_week(monkeypatch):
    monkeypatch.setenv("DEPENDENCY_WATCH_DIGEST_EVERY_WEEKS", "2")
    assert dw._digest_week_due(date(2026, 10, 21)) is False  # ISO week 43
    assert dw._digest_week_due(date(2026, 10, 28)) is True   # ISO week 44
    monkeypatch.setenv("DEPENDENCY_WATCH_DIGEST_EVERY_WEEKS", "1")
    assert dw._digest_week_due(date(2026, 10, 21)) is True


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------

def test_kev_names_only_the_versions_it_hits():
    """The first live scan: CVE-2023-4863 (libwebp) is in KEV and hits
    Pillow 10.0.0 in integrations, not 10.4.0 in core."""
    rows = [
        dw._row(_finding(key="CVE-2023-4863", kev=True, version="10.0.0",
                         repo="vomedjango-integrations-app", epss=0.9998),
                dw.WATCHLIST),
        dw._row(_finding(key="CVE-2026-112900", severity="CRITICAL",
                         epss=0.005), dw.WATCHLIST),
    ]
    line = dw._package_line("Pillow", rows, {})
    assert ("*CVE-2023-4863 is exploited in the wild (CISA KEV)*: "
            "10.0.0 in integrations.") in line
    assert "EPSS 100% for CVE-2023-4863" in line


def test_digest_reads_right_and_has_no_em_dash():
    inv = dw.Inventory(packages=[dw.Package("PyPI", "Pillow", "10.4.0",
                                            CORE.slug)],
                       scanned={CORE.slug}, unpinned={DB.slug: 2})
    findings = [
        _finding(severity="CRITICAL", kev=True, key="CVE-2026-112900"),
        _finding(repo="vomedjango-chats-app", severity="CRITICAL",
                 key="CVE-2026-112900"),
        _finding(package="shell-quote", eco="npm", severity="HIGH",
                 repo=WEB.slug, version="1.6.1", key="CVE-2"),
    ]
    rows = [dw._row(f, dw.WATCHLIST) for f in findings]
    eols = [dw.Eol("django", "4.2.20", "4.2", "eol", "2026-04-30", "4.2.24",
                   ["core", "chats"])]
    text, thread = dw.digest_messages(
        rows, {f.key: f.link for f in findings}, eols,
        ["CISA KEV did not answer"], inv, 3,
        [{"title": "Critical Pillow flaw", "url": "https://x", "hn": "h",
          "points": 120, "term": "Pillow"}],
        today=date(2026, 10, 28))
    assert "week of Oct 28" in text
    assert "*Pillow* 10.4.0 in chats, core: 1 critical, fixed in 13.0.0" \
        in text
    assert ("*CVE-2026-112900 is exploited in the wild (CISA KEV)*: "
            "10.4.0 in chats, core.") in text
    assert "*Django 4.2* (core, chats): no security fixes since 2026-04-30" \
        in text
    assert "CISA KEV did not answer" in text
    assert "database: 2 unpinned" in text
    assert "salesforce" in text
    assert "Critical Pillow flaw" in text
    assert "3 resolved since the last digest" in text
    assert "shell-quote" not in text and "shell-quote" in thread[0]
    for message in [text, *thread,
                    dw.alert_message(rows, {}, eols,
                                     {("Pillow", "onlyg"): "u"})]:
        assert "\N{EM DASH}" not in message
        assert "\N{EN DASH}" not in message
