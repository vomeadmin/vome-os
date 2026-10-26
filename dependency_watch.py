"""
dependency_watch.py

Tell #eng-infra when something Vome runs on has a known vulnerability.

WHY THIS EXISTS
---------------
On 2026-10-24 Sam read about a critical Pillow flaw (CVE-2026-112900, fixed in
13.0.0 on Oct 16) in the news. Core and chats pinned Pillow 10.4.0,
integrations 10.0.0, and chat image uploads hand user bytes straight to
`Image.open`. Nothing had told anyone. A one-off check that day also turned up
Django 4.2 (out of support since April 2026) in every backend and a vome-react
build still on Node 16.

Bitbucket has no Dependabot, so the four Django backends had no alerting at
all, and the GitHub repos only alert whoever happens to open the Security tab.
This is one checker for every repo, posting where the engineers already are.

WHERE THE DATA COMES FROM
-------------------------
  * Inventory: each repo's lockfile on its LIVE branch, as
    `mcp_connector/repos.py` (the list_repositories table) names it. Never
    main or master by assumption: core's `main` is a 2024 stub.
  * Matching: OSV.dev (`/v1/querybatch`, then `/v1/vulns/{id}`). Free, no
    key, and it carries GitHub's own advisories. NOT NVD: since April 2026
    NVD no longer analyses most open-source CVEs, so its severities are
    mostly blank for exactly the libraries we use.
  * NVD, only with NVD_API_KEY and only for a CVE OSV already matched but
    gave no severity. It fills the gap from whatever score NVD holds (the
    CNA's or CISA's, when NVD's own analysis is missing). Never discovery.
  * CISA KEV: exploited in the wild. FIRST EPSS: how likely exploitation is.
  * endoflife.date: whether Django, Python, Node, React, React Native,
    Postgres and Redis still get security fixes at the version we run.
  * Hacker News (Algolia search): headlines naming a package we lean on.
    A heads-up in the digest, never a finding on its own.

WHAT GETS SAID, AND WHEN
------------------------
  in CISA KEV                          -> alert the same morning + task
  critical with a fix, runtime code    -> alert the same morning + task
  high with a fix, heavily used pkg    -> alert the same morning + task
  EPSS at or above the threshold       -> alert the same morning + task
  framework or runtime already EOL     -> alert once + task, then every digest
  everything else (and EOL soon)       -> weekly digest

"Heavily used" is WATCHLIST below. A high advisory on a transitive build tool
(vome-react pulls in ~2,500 packages) waits for the digest: the first scan
found 346 high or critical hits, and alerting on all of them would teach the
channel to ignore this.

WHO OWNS IT
-----------
Sam's rule (2026-10-25): backend is OnlyG's, frontend and mobile are
Sanjay's. Core, database, chats, integrations and the servers' runtimes go
to OnlyG; the web app, landing page, mobile app and the web build to Sanjay.
The #eng-infra alert tags them with Sam copied. The weekly digest tags
nobody (the same rule as sentry_notify: a digest that pings people every
week gets muted).

ONE TASK PER ENGINEER
---------------------
Sam's rule (2026-10-26, after 25 separate tasks landed in one morning): each
engineer gets ONE task in the Priority Queue, "[Security] Backend dependency
upgrades" or "[Security] Frontend dependency upgrades", and every upgrade at
alert level is a subtask of it, named

    How urgent · the version we run → the one to move to · how big
    Critical (exploited) · Pillow 10.0.0, 10.4.0 → 13.0.0 · Major upgrade

  How urgent: Critical = in CISA KEV or EPSS at the threshold. High = a
    critical or high advisory in code we ship, or out of support. Low = a
    build tool (BUILD_TOOLS).
  How big, from the version numbers: Patch, Minor, Major upgrade, or
    Project (a major upgrade of a framework or runtime, FRAMEWORKS).

Critical, Major and Project upgrades get their own subtask; patch and minor
bumps (and build tools) share one "Quick upgrades" subtask with a checklist.
An upgrade is a package with its family (FAMILIES: react-pdf carries
pdfjs-dist) and its end-of-life cycle: Django's advisories and Django 4.2
being out of support are one subtask, one move to 5.2.

Every morning the watch rewrites what changed, ticks checklist lines and
closes subtasks once the live branch shows the upgrade, and closes the
parent when nothing is left. It only closes or relabels after a run that
read every repo of that engineer's and heard from every feed: unread is not
fixed. The tasks are marker rows in the ledger (PLAN, owner, slot). The
per-package tasks filed before this (Oct 25 and 26) were closed by the first
run after it, each with a comment linking its subtask.

The target is the release that clears every advisory the watch tracks on
that package, not the nearest fix for the one that triggered it: the first
KEV task said Pillow 10.0.1, which fixes CVE-2023-4863 and leaves 40 critical
and high advisories that 13.0.0 fixes. A target that is only a release
candidate says so.

CLAUDE READS, CODE DECIDES
--------------------------
Matching, severity, tiers and who gets alerted are all decided in code.
Claude only reads what was flagged (the advisory text OSV returned) and adds
a plain-English note in the thread: what the flaw is, what input reaches it,
what to do first. If Claude does not answer, the alert posts without it.

The daily scan runs every morning and is silent unless something new needs a
look. The digest runs weekly (DEPENDENCY_WATCH_DIGEST_EVERY_WEEKS=2 for every
other week).

FIRST RUN
---------
With an empty ledger nothing is "new", everything is. So the first run posts
one baseline digest instead of an alert per advisory, and records it all as
told. The upgrade tasks are still filed, and the baseline names them.

UNKNOWN IS NOT SAFE
-------------------
A repo whose lockfile could not be read, or a feed that did not answer, is
listed under "Not checked" in the digest. It is never reported as clean, and
an advisory in a repo that was not read this run is never marked resolved.

STATE
-----
`vomeos_dependency_alerts`: one row per (advisory, repo, package) with what
was last told. Re-alerts only when the severity goes up or the advisory joins
KEV. A row not seen in a repo that WAS scanned is marked resolved.

SWITCHES
--------
    DEPENDENCY_WATCH_ENABLED            default on. "false" stops both jobs
    SLACK_CHANNEL_ENG_INFRA             where it posts
    DEPENDENCY_WATCH_CLICKUP_LIST       upgrade tasks, default Priority Queue
    DEPENDENCY_WATCH_EPSS               alert threshold, default 0.1 (10%)
    DEPENDENCY_WATCH_DIGEST_EVERY_WEEKS 1 (weekly) or 2
    DEPENDENCY_WATCH_RUNTIMES           versions the repos cannot tell us, e.g.
                                        python=3.10,postgresql=14,redis=7.0
    DEPENDENCY_WATCH_PACKAGES           extra heavily-used packages, e.g.
                                        pypi:weasyprint,npm:dompurify
    DEPENDENCY_WATCH_CLAUDE             default on. "false" drops Claude's note
    NVD_API_KEY                         optional, fills missing severities

On demand:

    py dependency_watch.py            print the scan (no Slack, ClickUp, DB)
    py dependency_watch.py --digest   print the digest as it would post
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from vomeos import store

OSV = "https://api.osv.dev/v1"
KEV_URL = ("https://www.cisa.gov/sites/default/files/feeds/"
           "known_exploited_vulnerabilities.json")
EPSS_URL = "https://api.first.org/data/v1/epss"
EOL_URL = "https://endoflife.date/api/{product}.json"
HN_URL = "https://hn.algolia.com/api/v1/search_by_date"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_MAX = 40  # lookups per run; NVD allows 50 per 30 seconds with a key
CLICKUP_BASE = "https://api.clickup.com/api/v2"
TIMEOUT = 60

CHANNEL = os.environ.get("SLACK_CHANNEL_ENG_INFRA", "")
CLICKUP_LIST = os.environ.get("DEPENDENCY_WATCH_CLICKUP_LIST", "901113386257")
EOL_SOON_DAYS = 180

TABLE = "vomeos_dependency_alerts"

# Which files hold the inventory, per repo, on its live branch. Every path
# that exists is read; a repo with none of them is "not checked".
#
# Both requirements files for the backends: the deploy script lives on S3,
# not in the repo, so which one production installs cannot be read from here,
# and the two differ by a handful of lines in each repo.
MANIFESTS = {
    "vomedjango-restored-core-app": ("requirements.txt",
                                     "requirements_prod.txt"),
    "vomedjango-database-app": ("requirements/production.txt",),
    "vomedjango-chats-app": ("requirements.txt", "requirements_prod.txt"),
    "vomedjango-integrations-app": ("requirements.txt",
                                    "requirements_prod.txt"),
    "vome-react": ("package-lock.json",),
    "vome-landing-page": ("package-lock.json",),
    "VomeApp": ("package-lock.json", "yarn.lock"),
}
# Where a GitHub repo's production build pins Node.
NODE_WORKFLOW = ".github/workflows/cicd-prod.yml"

LABELS = {
    "vomedjango-restored-core-app": "core",
    "vomedjango-database-app": "database",
    "vomedjango-chats-app": "chats",
    "vomedjango-integrations-app": "integrations",
    "vomesalesforce": "salesforce",
    "vome-react": "web",
    "vome-landing-page": "landing",
    "VomeApp": "mobile",
}

# Heavily used: a HIGH advisory here alerts the same day, anywhere else it
# waits for the digest. Names are normalised (see _norm).
WATCHLIST = {
    ("PyPI", n) for n in (
        "django", "djangorestframework", "djangorestframework-simplejwt",
        "pillow", "pyjwt", "cryptography", "pyopenssl", "celery", "channels",
        "channels-redis", "daphne", "twisted", "gunicorn", "requests",
        "urllib3", "certifi", "lxml", "boto3", "botocore", "redis",
        "psycopg2", "psycopg2-binary", "openpyxl", "jinja2", "sqlparse",
    )
} | {
    ("npm", n) for n in (
        "react", "react-dom", "react-native", "expo", "axios", "jspdf",
        "xlsx", "pdfjs-dist", "react-pdf", "lodash", "moment", "i18next",
        "react-router-dom", "html-react-parser", "astro", "@sentry/react",
        "@sentry/react-native", "@stripe/stripe-js", "@azure/msal-browser",
    )
}

# Names searched on Hacker News. Display spellings, matched on word
# boundaries in the title together with a security word.
HN_TERMS = (
    "Django", "Pillow", "PyJWT", "cryptography", "Celery", "Gunicorn",
    "Daphne", "urllib3", "lxml", "Redis", "PostgreSQL", "Python", "Node.js",
    "npm", "React", "React Native", "Expo", "axios", "jsPDF", "SheetJS",
    "Astro",
)
_SECURITY_WORDS = re.compile(
    r"\b(cve|vulnerab\w*|exploit\w*|rce|flaw|security|malicious|malware|"
    r"compromis\w*|backdoor\w*|supply.chain|zero.day|0.day|injection)\b",
    re.I,
)

SEVERITY_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3,
                 "CRITICAL": 4}

KEV, URGENT, DIGEST = "kev", "urgent", "digest"


def _flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() not in (
        "0", "false", "no", "off", "")


def _enabled() -> bool:
    return _flag("DEPENDENCY_WATCH_ENABLED")


def _epss_threshold() -> float:
    try:
        return float(os.environ.get("DEPENDENCY_WATCH_EPSS", "0.1"))
    except ValueError:
        return 0.1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(ecosystem: str, name: str) -> str:
    """PyPI treats `Django_Rest`, `django.rest` and `django-rest` as one
    package (PEP 503). npm names are already canonical."""
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name.lower()


def _watchlist() -> set[tuple[str, str]]:
    extra = set()
    for item in os.environ.get("DEPENDENCY_WATCH_PACKAGES", "").split(","):
        eco, _, name = item.strip().partition(":")
        eco = {"pypi": "PyPI", "npm": "npm"}.get(eco.lower())
        if eco and name.strip():
            extra.add((eco, _norm(eco, name.strip())))
    return WATCHLIST | extra


def _label(slug: str) -> str:
    return LABELS.get(slug, slug)


# Who fixes what (see WHO OWNS IT). A label, so end-of-life rows, which are
# keyed by where ("web build", "servers"), route the same way as packages.
FRONTEND = {"web", "landing", "mobile", "web build", "landing build",
            "mobile build"}
OWNER_NAMES = {"onlyg": "OnlyG", "sanjay": "Sanjay"}


def owner_of(repo: str) -> str:
    """"onlyg" or "sanjay" for a repo slug or a label like "web build"."""
    return "sanjay" if _label(repo) in FRONTEND else "onlyg"


def _owners(repos) -> list[str]:
    return sorted({owner_of(r) for r in repos}, key=list(OWNER_NAMES).index)


def _mention(name: str) -> str:
    """<@U...> for onlyg, sanjay or sam; the plain name when no id is set."""
    import sentry_notify

    return sentry_notify.mention(name)


def _clickup_user(owner: str) -> int | None:
    import sprint_thread

    engineer = sprint_thread.ENGINEERS.get(owner)
    return engineer[0] if engineer else None


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Package:
    ecosystem: str  # "PyPI" | "npm"
    name: str
    version: str
    repo: str
    dev: bool = False  # npm devDependency: never shipped to a user


def parse_requirements(text: str) -> tuple[list[tuple[str, str]], list[str],
                                           int]:
    """(pins, `-r` includes, unpinned count).

    Only `==` pins can be matched exactly. A range (`celery`, `>=5`) is
    counted so the digest can say it was not checked, rather than dropped.
    """
    pins, includes, unpinned = [], [], 0
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r", "--requirement")):
            target = line.split(None, 1)[-1] if " " in line else line[2:]
            includes.append(target.strip())
            continue
        if line.startswith("-"):
            continue  # -e, --index-url and friends
        line = line.split(";", 1)[0].strip()
        m = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)(?:\[[^\]]*\])?\s*==\s*([^\s,]+)$",
            line,
        )
        if m:
            pins.append((m.group(1), m.group(2)))
        else:
            unpinned += 1
    return pins, includes, unpinned


def parse_package_lock(lock: dict) -> list[tuple[str, str, bool]]:
    """(name, version, dev) from lockfile v2/v3 `packages`, or v1
    `dependencies` when that is all there is."""
    out = []
    packages = lock.get("packages")
    if isinstance(packages, dict):
        for key, meta in packages.items():
            if "node_modules/" not in key or not isinstance(meta, dict):
                continue
            version = meta.get("version")
            if not version or meta.get("link"):
                continue
            name = meta.get("name") or key.rsplit("node_modules/", 1)[-1]
            out.append((name, version, bool(meta.get("dev"))))
        return out

    def walk(deps: dict):
        for name, meta in (deps or {}).items():
            if isinstance(meta, dict) and meta.get("version"):
                out.append((name, meta["version"], bool(meta.get("dev"))))
                walk(meta.get("dependencies") or {})

    walk(lock.get("dependencies") or {})
    return out


def parse_yarn_lock(text: str) -> list[tuple[str, str, bool]]:
    """(name, version, False) from a yarn v1 or berry lockfile. Yarn does not
    record dev-ness, so every entry counts as shipped."""
    out = []
    name = ""
    for line in (text or "").splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            spec = line.rstrip(":").split(",")[0].strip().strip('"')
            spec = spec.split("@npm:")[0] if "@npm:" in spec else spec
            # "@scope/pkg@^1.0.0" -> "@scope/pkg"; "pkg@^1" -> "pkg"
            at = spec.rfind("@")
            name = spec[:at] if at > 0 else spec
            if name.startswith("__"):
                name = ""  # berry's __metadata block, not a package
            continue
        m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?\s*$', line)
        if m and name:
            out.append((name, m.group(1), False))
            name = ""
    return out


def _read(repo, path: str):
    """(data, error). JSON comes back parsed, everything else as text."""
    from vomeos.integrations.code_search import Bitbucket, GitHub

    # Its own allowlist, like the claude.ai connector: VOMEOS_CODE_REPOS
    # keeps the Sentry analyst narrow and leaves out the landing page, which
    # this watch must still read. Lockfiles only, never code.
    allowlist = tuple(MANIFESTS)
    if repo.host == "bitbucket":
        result = Bitbucket(allowlist=allowlist).read_file(
            repo.slug, path, ref=repo.live_ref, max_bytes=50_000_000)
    else:
        result = GitHub(allowlist=allowlist).read_file(
            repo.slug, path, ref=repo.live_ref, owner=repo.owner,
            max_bytes=50_000_000, raw=True)
    if not result.ok:
        return None, (f"{result.status_code}" if result.status_code == 404
                      else (result.error or "unreadable")[:160])
    return result.data, ""


def _as_text(data) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data) if data is not None else ""


def _as_json(data) -> dict:
    if isinstance(data, dict):
        return data
    try:
        parsed = json.loads(data or "")
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


@dataclass
class Inventory:
    packages: list[Package] = field(default_factory=list)
    scanned: set[str] = field(default_factory=set)       # repos read OK
    problems: list[str] = field(default_factory=list)     # "Not checked"
    node: dict[str, str] = field(default_factory=dict)    # repo -> version
    unpinned: dict[str, int] = field(default_factory=dict)


def _python_inventory(repo, inv: Inventory) -> bool:
    errors_before = len(inv.problems)
    found = False
    pins: set[tuple[str, str]] = set()
    unpinned = 0
    queue = list(MANIFESTS.get(repo.slug, ()))
    seen: set[str] = set()
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        data, error = _read(repo, path)
        if error:
            if error != "404":
                inv.problems.append(
                    f"{_label(repo.slug)}: could not read {path} ({error})")
            continue
        found = True
        got, includes, loose = parse_requirements(_as_text(data))
        pins.update(got)
        unpinned += loose
        base = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        queue.extend(base + inc for inc in includes)
    for name, version in sorted(pins):
        inv.packages.append(Package("PyPI", name, version, repo.slug))
    if unpinned:
        inv.unpinned[repo.slug] = unpinned
    if not found and len(inv.problems) == errors_before:
        inv.problems.append(f"{_label(repo.slug)}: no requirements file on "
                            f"{repo.live_ref}")
    # A file that could not be read means the repo was only partly seen, so
    # its old findings must not be marked resolved.
    return found and len(inv.problems) == errors_before


def _npm_inventory(repo, inv: Inventory) -> bool:
    manifest = {}
    data, error = _read(repo, "package.json")
    if not error:
        manifest = _as_json(data)
    dev_direct = set((manifest.get("devDependencies") or {}))

    for path in MANIFESTS.get(repo.slug, ()):
        data, error = _read(repo, path)
        if error == "404":
            continue
        if error:
            inv.problems.append(
                f"{_label(repo.slug)}: could not read {path} ({error})")
            return False
        if path.endswith("package-lock.json"):
            entries = parse_package_lock(_as_json(data))
        else:
            entries = parse_yarn_lock(_as_text(data))
        if not entries:
            inv.problems.append(
                f"{_label(repo.slug)}: {path} had no packages in it")
            return False
        for name, version, dev in dict.fromkeys(entries):
            inv.packages.append(Package(
                "npm", name, version, repo.slug,
                dev=dev or name in dev_direct,
            ))
        break
    else:
        inv.problems.append(f"{_label(repo.slug)}: no lockfile on "
                            f"{repo.live_ref}")
        return False

    if repo.host == "github":
        data, error = _read(repo, NODE_WORKFLOW)
        if not error:
            m = re.search(r"NODE_VERSION:\s*[\"']?([0-9][0-9.]*)",
                          _as_text(data))
            if m:
                inv.node[repo.slug] = m.group(1)
    return True


def build_inventory(repos=None) -> Inventory:
    from mcp_connector import repos as repo_table

    inv = Inventory()
    for repo in repos or repo_table.all_repos():
        if repo.slug not in MANIFESTS:
            continue
        try:
            if any(p.endswith(".txt") for p in MANIFESTS[repo.slug]):
                ok = _python_inventory(repo, inv)
            else:
                ok = _npm_inventory(repo, inv)
        except Exception as exc:
            inv.problems.append(f"{_label(repo.slug)}: {str(exc)[:160]}")
            ok = False
        if ok:
            inv.scanned.add(repo.slug)
    return inv


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

_CVSS3 = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PR = {False: {"N": 0.85, "L": 0.62, "H": 0.27},
       True: {"N": 0.85, "L": 0.68, "H": 0.5}}


def _roundup(value: float) -> float:
    """CVSS 3.1's Roundup: the smallest one-decimal number >= value."""
    i = round(value * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10


def cvss3_score(vector: str) -> float | None:
    """CVSS v3.x base score from its vector, or None if it is not one.

    Only needed when an advisory has no severity label: PyPA records carry a
    vector and nothing else, and a v3 vector is enough to rank them.
    """
    if not (vector or "").startswith("CVSS:3"):
        return None
    try:
        m = dict(p.split(":", 1) for p in vector.split("/")[1:])
        changed = m["S"] == "C"
        iss = 1 - ((1 - _CVSS3["CIA"][m["C"]]) * (1 - _CVSS3["CIA"][m["I"]])
                   * (1 - _CVSS3["CIA"][m["A"]]))
        impact = (7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
                  if changed else 6.42 * iss)
        exploitability = (8.22 * _CVSS3["AV"][m["AV"]] * _CVSS3["AC"][m["AC"]]
                          * _PR[changed][m["PR"]] * _CVSS3["UI"][m["UI"]])
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    total = impact + exploitability
    return _roundup(min(1.08 * total if changed else total, 10))


def _rating(score: float) -> str:
    if score >= 9:
        return "CRITICAL"
    if score >= 7:
        return "HIGH"
    if score >= 4:
        return "MEDIUM"
    return "LOW" if score > 0 else "UNKNOWN"


def severity_of(record: dict) -> str:
    vid = record.get("id", "")
    if vid.startswith("MAL-"):
        return "CRITICAL"  # a malicious package, not a bug in a good one
    label = str((record.get("database_specific") or {}).get("severity")
                or "").upper()
    if label == "MODERATE":
        label = "MEDIUM"
    if label in SEVERITY_RANK and label != "UNKNOWN":
        return label
    best = "UNKNOWN"
    for s in record.get("severity") or []:
        score = cvss3_score(str(s.get("score") or ""))
        if score is not None and SEVERITY_RANK[_rating(score)] > \
                SEVERITY_RANK[best]:
            best = _rating(score)
    return best


def advisory_key(record: dict) -> str:
    """One stable name per vulnerability. OSV returns the same Pillow bug as
    a GHSA record and a PYSEC record; both alias the CVE, and a CVE never
    changes, so it is the key whenever there is one."""
    ids = [record.get("id", "")] + list(record.get("aliases") or [])
    cves = sorted(i for i in ids if i.startswith("CVE-"))
    if cves:
        return cves[0]
    ghsa = sorted(i for i in ids if i.startswith("GHSA-"))
    return ghsa[0] if ghsa else record.get("id", "")


def _vkey(version: str) -> tuple:
    """A loose version order, good enough to pick the nearest fix."""
    core = version.split("+", 1)[0]
    nums = [int(n) for n in re.findall(r"\d+", core)][:5]
    nums += [0] * (5 - len(nums))
    pre = bool(re.search(r"(a|b|rc|alpha|beta|dev|pre|canary|next)\d*",
                         core.lower().split(".")[-1]))
    return (*nums, 0 if pre else 1)


def fixed_in(records: list[dict], ecosystem: str, name: str,
             version: str) -> str:
    """The nearest release above `version` that fixes every record given."""
    want = _norm(ecosystem, name)
    current = _vkey(version)
    best = ""
    for record in records:
        fixes = []
        for affected in record.get("affected") or []:
            pkg = affected.get("package") or {}
            if pkg.get("ecosystem") != ecosystem or \
                    _norm(ecosystem, pkg.get("name", "")) != want:
                continue
            for rng in affected.get("ranges") or []:
                for event in rng.get("events") or []:
                    fix = event.get("fixed")
                    if fix and _vkey(fix) > current:
                        fixes.append(fix)
        if not fixes:
            # One record with no fix: no single upgrade fixes them all.
            return ""
        nearest = min(fixes, key=_vkey)
        if not best or _vkey(nearest) > _vkey(best):
            best = nearest
    return best


# ---------------------------------------------------------------------------
# Matching and enrichment
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    key: str             # CVE, else GHSA, else OSV id
    ids: list[str]       # every OSV id that matched, for links
    ecosystem: str
    package: str
    version: str
    repo: str
    severity: str
    summary: str
    fixed: str
    dev: bool = False
    kev: bool = False
    kev_added: str = ""
    epss: float | None = None
    details: str = ""    # the advisory text, for Claude's note only

    @property
    def link(self) -> str:
        ghsa = [i for i in self.ids if i.startswith("GHSA-")]
        return f"https://osv.dev/vulnerability/{(ghsa or self.ids)[0]}"


def _post_json(url: str, body: dict) -> dict:
    resp = httpx.post(url, json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get_json(url: str, params: dict | None = None):
    resp = httpx.get(url, params=params, timeout=TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def osv_ids(packages: list[Package]) -> dict[tuple[str, str, str], list[str]]:
    """(ecosystem, name, version) -> OSV ids that affect it. Raises when OSV
    does not answer: an unanswered query is not a clean result."""
    unique = list(dict.fromkeys((p.ecosystem, p.name, p.version)
                                for p in packages))
    out: dict[tuple[str, str, str], list[str]] = {}
    for start in range(0, len(unique), 1000):
        chunk = unique[start:start + 1000]
        data = _post_json(f"{OSV}/querybatch", {"queries": [
            {"package": {"ecosystem": e, "name": n}, "version": v}
            for e, n, v in chunk
        ]})
        for query, result in zip(chunk, data.get("results") or []):
            ids = [v["id"] for v in result.get("vulns") or []]
            token = result.get("next_page_token")
            while token:
                e, n, v = query
                more = _post_json(f"{OSV}/query", {
                    "package": {"ecosystem": e, "name": n}, "version": v,
                    "page_token": token,
                })
                ids += [x["id"] for x in more.get("vulns") or []]
                token = more.get("next_page_token")
            if ids:
                out[query] = ids
    return out


def osv_records(ids) -> dict[str, dict]:
    def one(vid: str):
        for attempt in range(3):
            try:
                return vid, _get_json(f"{OSV}/vulns/{vid}")
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, sorted(set(ids))))


def load_kev() -> dict[str, str] | None:
    """CVE -> date added, or None when CISA did not answer."""
    try:
        data = _get_json(KEV_URL)
    except Exception as exc:
        print(f"[DEPWATCH] KEV unavailable: {exc}")
        return None
    return {v.get("cveID", ""): v.get("dateAdded", "")
            for v in data.get("vulnerabilities") or []}


def load_epss(cves: list[str]) -> dict[str, float] | None:
    out: dict[str, float] = {}
    try:
        for start in range(0, len(cves), 50):
            chunk = cves[start:start + 50]
            data = _get_json(EPSS_URL, {"cve": ",".join(chunk),
                                        "limit": len(chunk)})
            for row in data.get("data") or []:
                out[row["cve"]] = float(row.get("epss") or 0)
    except Exception as exc:
        print(f"[DEPWATCH] EPSS unavailable: {exc}")
        return None
    return out


def match(inv: Inventory) -> tuple[list[Finding], list[str]]:
    """Every (advisory, repo, package) affecting the inventory, enriched.
    Returns the findings and anything that could not be checked."""
    problems: list[str] = []
    try:
        hits = osv_ids(inv.packages)
        records = osv_records(i for ids in hits.values() for i in ids)
    except Exception as exc:
        return [], [f"OSV.dev did not answer, nothing was matched "
                    f"({str(exc)[:160]})"]

    findings: list[Finding] = []
    for pkg in inv.packages:
        ids = hits.get((pkg.ecosystem, pkg.name, pkg.version)) or []
        grouped: dict[str, list[dict]] = {}
        for vid in ids:
            record = records.get(vid)
            if record and not record.get("withdrawn"):
                grouped.setdefault(advisory_key(record), []).append(record)
        for key, recs in grouped.items():
            severity = max((severity_of(r) for r in recs),
                           key=SEVERITY_RANK.get)
            summary = next((r.get("summary") for r in recs
                            if r.get("summary")), "") or \
                (recs[0].get("details") or "").split("\n")[0]
            details = max((r.get("details") or "" for r in recs), key=len)
            findings.append(Finding(
                key=key, ids=[r["id"] for r in recs],
                ecosystem=pkg.ecosystem, package=pkg.name,
                version=pkg.version, repo=pkg.repo, severity=severity,
                summary=summary[:160],
                fixed=fixed_in(recs, pkg.ecosystem, pkg.name, pkg.version),
                dev=pkg.dev, details=details[:1500],
            ))

    cves = sorted({f.key for f in findings if f.key.startswith("CVE-")})
    kev = load_kev()
    if kev is None:
        problems.append("CISA KEV did not answer, exploited-in-the-wild "
                        "status not checked this run")
    epss = load_epss(cves) if cves else {}
    if epss is None:
        problems.append("FIRST EPSS did not answer, exploit likelihood not "
                        "checked this run")
    for f in findings:
        if kev and f.key in kev:
            f.kev, f.kev_added = True, kev[f.key]
        if epss:
            f.epss = epss.get(f.key)

    blank = sorted({f.key for f in findings
                    if f.severity == "UNKNOWN" and f.key.startswith("CVE-")})
    if blank and os.environ.get("NVD_API_KEY"):
        filled, failed = nvd_severity(blank[:NVD_MAX])
        for f in findings:
            if f.severity == "UNKNOWN" and filled.get(f.key):
                f.severity = filled[f.key]
        if failed:
            problems.append(f"NVD did not answer for {failed} CVE(s), their "
                            "severity is still unknown")
    return findings, problems


def nvd_severity(cves: list[str]) -> tuple[dict[str, str], int]:
    """CVE -> severity from NVD, for CVEs OSV matched without one. Returns
    the severities found and how many lookups failed. Prefers NVD's own
    (Primary) score, then any other source NVD lists, newest CVSS first."""
    out: dict[str, str] = {}
    failed = 0
    headers = {"apiKey": os.environ.get("NVD_API_KEY", "")}
    for i, cve in enumerate(cves):
        if i:
            time.sleep(0.7)
        try:
            resp = httpx.get(NVD_URL, params={"cveId": cve}, headers=headers,
                             timeout=TIMEOUT)
            resp.raise_for_status()
            vulns = resp.json().get("vulnerabilities") or []
        except Exception as exc:
            print(f"[DEPWATCH] NVD lookup for {cve} failed: {exc}")
            failed += 1
            continue
        metrics = ((vulns[0].get("cve") or {}).get("metrics") or {}
                   if vulns else {})
        for version in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
            scored = sorted(metrics.get(version) or [],
                            key=lambda m: m.get("type") != "Primary")
            label = next((str((m.get("cvssData") or {})
                              .get("baseSeverity") or "").upper()
                          for m in scored), "")
            if label in SEVERITY_RANK and label != "UNKNOWN":
                out[cve] = label
                break
    return out, failed


def tier(f: Finding, watchlist: set | None = None) -> str:
    if f.kev:
        return KEV
    if f.dev:
        return DIGEST
    if f.epss is not None and f.epss >= _epss_threshold():
        return URGENT
    if not f.fixed:
        return DIGEST
    if f.severity == "CRITICAL":
        return URGENT
    heavy = (f.ecosystem, _norm(f.ecosystem, f.package)) in (
        watchlist if watchlist is not None else _watchlist())
    if f.severity == "HIGH" and heavy:
        return URGENT
    return DIGEST


# ---------------------------------------------------------------------------
# End of life
# ---------------------------------------------------------------------------

@dataclass
class Eol:
    product: str
    version: str
    cycle: str
    status: str      # "eol" | "soon" | "ok" | "unlisted"
    eol_date: str
    latest: str
    where: list[str]
    upgrade_to: str = ""  # the newest supported cycle, "24 LTS"

    @property
    def key(self) -> str:
        return f"EOL:{self.product}:{self.cycle or self.version}"


EOL_PRODUCTS = {
    # product -> (ecosystem, package) whose pin gives the version
    "django": ("PyPI", "django"),
    "react": ("npm", "react"),
    "react-native": ("npm", "react-native"),
}
EOL_NAMES = {"django": "Django", "react": "React",
             "react-native": "React Native", "nodejs": "Node.js",
             "python": "Python", "postgresql": "PostgreSQL",
             "redis": "Redis"}


def _runtimes() -> dict[str, str]:
    out = {}
    for item in os.environ.get("DEPENDENCY_WATCH_RUNTIMES", "").split(","):
        product, _, version = item.partition("=")
        if product.strip() and version.strip():
            out[product.strip().lower()] = version.strip()
    return out


def _upgrade_cycle(cycles: list[dict], today: date) -> str:
    """The cycle to move to: the newest one supported past the warning
    window, and an LTS one when the product has them ("24 LTS", not Node 25,
    which is out of support in months)."""
    def supported(c: dict) -> bool:
        eol = c.get("eol")
        if isinstance(eol, str):
            try:
                return date.fromisoformat(eol) > today + timedelta(
                    days=EOL_SOON_DAYS)
            except ValueError:
                return False
        return eol is not True

    def lts(c: dict) -> bool:
        value = c.get("lts")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value) <= today
            except ValueError:
                return False
        return bool(value)

    has_lts = any(lts(c) for c in cycles)
    for c in sorted(cycles, key=lambda c: _vkey(str(c.get("cycle", ""))),
                    reverse=True):
        if supported(c) and (lts(c) or not has_lts):
            return str(c.get("cycle", "")) + (" LTS" if has_lts else "")
    return ""


def _cycle_for(version: str, cycles: list[dict]) -> dict | None:
    best = None
    for c in cycles:
        name = str(c.get("cycle", ""))
        if version == name or version.startswith(name + "."):
            if best is None or len(name) > len(str(best.get("cycle", ""))):
                best = c
    return best


def check_eol(inv: Inventory, today: date | None = None
              ) -> tuple[list[Eol], list[str]]:
    today = today or date.today()
    wanted: dict[tuple[str, str], set[str]] = {}
    for product, (eco, name) in EOL_PRODUCTS.items():
        for p in inv.packages:
            if p.ecosystem == eco and _norm(eco, p.name) == name and not p.dev:
                wanted.setdefault((product, p.version), set()).add(
                    _label(p.repo))
    for repo, version in inv.node.items():
        wanted.setdefault(("nodejs", version), set()).add(
            f"{_label(repo)} build")
    runtimes = _runtimes()
    for product, version in runtimes.items():
        wanted.setdefault((product, version), set()).add("servers")

    problems = []
    missing = [EOL_NAMES[p] for p in ("python", "postgresql", "redis")
               if p not in runtimes]
    if missing:
        problems.append(
            f"{', '.join(missing)}: version unknown, set "
            "DEPENDENCY_WATCH_RUNTIMES (e.g. python=3.10,postgresql=14)")

    cache: dict[str, list[dict] | None] = {}
    out: list[Eol] = []
    for (product, version), where in sorted(wanted.items()):
        if product not in cache:
            try:
                cache[product] = _get_json(EOL_URL.format(product=product))
            except Exception as exc:
                cache[product] = None
                problems.append(f"endoflife.date had no answer for "
                                f"{product} ({str(exc)[:80]})")
        cycles = cache[product]
        if cycles is None:
            continue
        cycle = _cycle_for(version, cycles)
        if cycle is None:
            out.append(Eol(product, version, "", "unlisted", "", "",
                           sorted(where)))
            continue
        eol = cycle.get("eol")
        status, eol_date = "ok", ""
        if eol is True:
            status = "eol"
        elif isinstance(eol, str):
            eol_date = eol
            try:
                when = date.fromisoformat(eol)
                if when <= today:
                    status = "eol"
                elif when <= today + timedelta(days=EOL_SOON_DAYS):
                    status = "soon"
            except ValueError:
                pass
        name = str(cycle.get("cycle", ""))
        same = next((e for e in out
                     if (e.product, e.cycle) == (product, name)), None)
        if same:
            # 4.2.20 in core and 4.2.1 in database are one support cycle,
            # so one line and one alert, not one per patch release.
            same.where = sorted(set(same.where) | where)
            continue
        out.append(Eol(product, version, name, status, eol_date,
                       str(cycle.get("latest") or ""), sorted(where),
                       _upgrade_cycle(cycles, today)
                       if status != "ok" else ""))
    return out, problems


# ---------------------------------------------------------------------------
# Hacker News
# ---------------------------------------------------------------------------

def hn_chatter(days: int = 8) -> list[dict] | None:
    """Recent HN stories that name a watched package and a security word.
    A heads-up, not a finding: the scan above is what decides."""
    since = int((_now() - timedelta(days=days)).timestamp())
    seen: dict[str, dict] = {}  # by link, so a story posted twice shows once
    try:
        for term in HN_TERMS:
            data = _get_json(HN_URL, {
                "query": term, "tags": "story", "hitsPerPage": 30,
                "numericFilters": f"created_at_i>{since}",
            })
            pattern = re.compile(rf"(?<![\w.]){re.escape(term)}(?![\w])",
                                 re.I)
            for hit in data.get("hits") or []:
                title = hit.get("title") or ""
                if not (pattern.search(title)
                        and _SECURITY_WORDS.search(title)):
                    continue
                story = {
                    "title": title, "term": term,
                    "url": hit.get("url") or "",
                    "points": int(hit.get("points") or 0),
                    "hn": ("https://news.ycombinator.com/item?id="
                           f"{hit['objectID']}"),
                }
                link = story["url"].split("?", 1)[0] or story["hn"]
                if story["points"] > seen.get(link, {}).get("points", -1):
                    seen[link] = story
    except Exception as exc:
        print(f"[DEPWATCH] Hacker News unavailable: {exc}")
        return None
    return sorted(seen.values(), key=lambda h: -h["points"])[:8]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

store.register_table(
    TABLE,
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        advisory_key     VARCHAR NOT NULL,
        repo             VARCHAR NOT NULL,
        package          VARCHAR NOT NULL,
        version          VARCHAR,
        severity         VARCHAR,
        in_kev           BOOLEAN NOT NULL DEFAULT FALSE,
        epss             DOUBLE PRECISION,
        fixed_in         VARCHAR,
        tier             VARCHAR,
        summary          TEXT,
        first_seen_at    TIMESTAMP NOT NULL DEFAULT NOW(),
        last_seen_at     TIMESTAMP NOT NULL DEFAULT NOW(),
        resolved_at      TIMESTAMP,
        alerted_at       TIMESTAMP,
        alerted_severity VARCHAR,
        alerted_kev      BOOLEAN,
        clickup_task_id  VARCHAR,
        PRIMARY KEY (advisory_key, repo, package)
    )
    """,
)


@dataclass
class Row:
    """One ledger row's identity and the facts the alert rules read."""
    key: str
    repo: str
    package: str
    version: str
    severity: str
    kev: bool = False
    epss: float | None = None
    fixed: str = ""
    tier: str = DIGEST
    summary: str = ""

    @property
    def ident(self) -> tuple[str, str, str]:
        return (self.key, self.repo, self.package)


def _row(f: Finding, watchlist: set) -> Row:
    return Row(f.key, f.repo, f.package, f.version, f.severity, f.kev,
               f.epss, f.fixed, tier(f, watchlist), f.summary)


def _eol_rows(eols: list[Eol]) -> list[Row]:
    rows = []
    for e in eols:
        if e.status not in ("eol", "soon"):
            continue
        for where in e.where:
            rows.append(Row(e.key, where, e.product, e.version,
                            "EOL" if e.status == "eol" else "EOL_SOON",
                            fixed=e.upgrade_to,
                            tier=URGENT if e.status == "eol" else DIGEST,
                            summary=f"supported until {e.eol_date or 'n/a'}"))
    return rows


def needs_alert(row: Row, told: dict | None) -> bool:
    """New, escalated, or newly exploited. Never the same news twice."""
    if row.tier not in (KEV, URGENT):
        return False
    if not told or not told.get("alerted_at"):
        return True
    if row.kev and not told.get("alerted_kev"):
        return True
    before = SEVERITY_RANK.get(str(told.get("alerted_severity") or ""), -1)
    return SEVERITY_RANK.get(row.severity, -1) > before


class Ledger:
    """What was seen and what was said. `available` False means no database:
    the scan still runs and the digest still posts, but same-day alerts are
    held, because without memory they would repeat every morning."""

    def __init__(self, enabled: bool = True):
        self.available = enabled and store.ensure_table(TABLE)

    def told(self) -> dict[tuple[str, str, str], dict]:
        if not self.available:
            return {}
        rows = store.query(f"SELECT * FROM {TABLE}")
        return {(r["advisory_key"], r["repo"], r["package"]): r for r in rows}

    # A marker row rather than "is the table empty": the first run records
    # every finding before it posts, so if that post fails the table is full
    # and the next morning would alert on all of it one by one.
    _BASELINE = ("BASELINE", "-", "-")

    def baseline_done(self) -> bool:
        if not self.available:
            return True
        return self._BASELINE in self.told()

    def mark_baseline(self) -> None:
        if self.available:
            store.execute(
                f"INSERT INTO {TABLE} (advisory_key, repo, package, "
                "alerted_at) VALUES (:k, :r, :p, NOW()) ON CONFLICT DO "
                "NOTHING",
                dict(zip(("k", "r", "p"), self._BASELINE)),
            )

    def set_task(self, rows: list[Row], task_id: str) -> None:
        """Which subtask holds each row's upgrade."""
        if not self.available or not task_id:
            return
        for r in rows:
            store.execute(
                f"UPDATE {TABLE} SET clickup_task_id = :task WHERE "
                "advisory_key = :k AND repo = :r AND package = :p",
                {"task": task_id, "k": r.key, "r": r.repo, "p": r.package},
            )

    def clear_task(self, task_id: str) -> None:
        """Forget a task that was closed, so it is never closed again."""
        if self.available and task_id:
            store.execute(
                f"UPDATE {TABLE} SET clickup_task_id = NULL WHERE "
                "clickup_task_id = :task AND advisory_key <> :plan",
                {"task": task_id, "plan": self._PLAN},
            )

    # The upgrade plan's tasks, as marker rows: (PLAN, owner, slot), where
    # slot is an upgrade, "quick" or "*" (the owner's parent task). The
    # summary column holds a fingerprint of what was last written to it, so
    # an unchanged task is not rewritten every morning.
    _PLAN = "PLAN"

    def plan(self) -> dict[tuple[str, str], dict]:
        if not self.available:
            return {}
        rows = store.query(
            f"SELECT repo, package, clickup_task_id, summary FROM {TABLE} "
            "WHERE advisory_key = :plan", {"plan": self._PLAN})
        return {(r["repo"], r["package"]): dict(r) for r in rows}

    def set_plan(self, owner: str, slot: str, task_id: str,
                 fingerprint: str) -> None:
        """Kept the moment the task exists, before Slack is tried, so a
        failed post never leads to a second task for the same upgrade."""
        if self.available:
            store.execute(
                f"INSERT INTO {TABLE} (advisory_key, repo, package, "
                "clickup_task_id, summary) VALUES (:plan, :o, :s, :task, "
                ":fp) ON CONFLICT (advisory_key, repo, package) DO UPDATE "
                "SET clickup_task_id = EXCLUDED.clickup_task_id, "
                "summary = EXCLUDED.summary",
                {"plan": self._PLAN, "o": owner, "s": slot, "task": task_id,
                 "fp": fingerprint},
            )

    def drop_plan(self, owner: str, slot: str) -> None:
        if self.available:
            store.execute(
                f"DELETE FROM {TABLE} WHERE advisory_key = :plan AND "
                "repo = :o AND package = :s",
                {"plan": self._PLAN, "o": owner, "s": slot})

    def record(self, rows: list[Row]) -> None:
        if not self.available:
            return
        for r in rows:
            store.execute(
                f"INSERT INTO {TABLE} (advisory_key, repo, package, version, "
                "severity, in_kev, epss, fixed_in, tier, summary) VALUES "
                "(:key, :repo, :package, :version, :severity, :kev, :epss, "
                ":fixed, :tier, :summary) "
                "ON CONFLICT (advisory_key, repo, package) DO UPDATE SET "
                "version = EXCLUDED.version, severity = EXCLUDED.severity, "
                "in_kev = EXCLUDED.in_kev, epss = EXCLUDED.epss, "
                "fixed_in = EXCLUDED.fixed_in, tier = EXCLUDED.tier, "
                "summary = EXCLUDED.summary, last_seen_at = NOW(), "
                "resolved_at = NULL",
                {"key": r.key, "repo": r.repo, "package": r.package,
                 "version": r.version, "severity": r.severity, "kev": r.kev,
                 "epss": r.epss, "fixed": r.fixed, "tier": r.tier,
                 "summary": r.summary},
            )

    def resolve_missing(self, seen: list[Row], scanned: set[str]) -> int:
        """Open rows in a repo that WAS read this run but no longer show up:
        the package was upgraded. Repos that could not be read are left
        alone, because unread is not fixed."""
        if not self.available or not scanned:
            return 0
        current = {r.ident for r in seen}
        resolved = 0
        for ident, row in self.told().items():
            if row.get("resolved_at") or ident in current:
                continue
            if row["repo"] not in scanned or ident[0].startswith("EOL:"):
                continue
            if store.execute(
                f"UPDATE {TABLE} SET resolved_at = NOW() WHERE "
                "advisory_key = :k AND repo = :r AND package = :p",
                {"k": ident[0], "r": ident[1], "p": ident[2]},
            ):
                resolved += 1
        return resolved

    def resolved_since(self, since: datetime) -> int:
        if not self.available:
            return 0
        rows = store.query(
            f"SELECT COUNT(*) AS n FROM {TABLE} WHERE resolved_at >= :since",
            {"since": since.replace(tzinfo=None)},
        )
        return int(rows[0]["n"]) if rows else 0

    def mark_alerted(self, rows: list[Row]) -> None:
        if not self.available:
            return
        for r in rows:
            store.execute(
                f"UPDATE {TABLE} SET alerted_at = NOW(), "
                "alerted_severity = :sev, alerted_kev = :kev "
                "WHERE advisory_key = :k AND repo = :r AND package = :p",
                {"sev": r.severity, "kev": r.kev,
                 "k": r.key, "r": r.repo, "p": r.package},
            )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _repo_versions(rows: list[Row]) -> str:
    """"10.4.0 in core, chats; 10.0.0 in integrations"."""
    by_version: dict[str, set[str]] = {}
    for r in rows:
        by_version.setdefault(r.version, set()).add(_label(r.repo))
    return "; ".join(f"{v} in {', '.join(sorted(where))}"
                     for v, where in sorted(by_version.items(),
                                            key=lambda kv: _vkey(kv[0]),
                                            reverse=True))


def _tally(rows: list[Row]) -> str:
    """"5 critical, 35 high": advisories by their worst severity."""
    counts: dict[str, int] = {}
    for k in {r.key for r in rows}:
        sev = max((r.severity for r in rows if r.key == k),
                  key=lambda s: SEVERITY_RANK.get(s, 0))
        counts[sev] = counts.get(sev, 0) + 1
    return ", ".join(f"{n} {s.lower()}" for s, n in
                     sorted(counts.items(),
                            key=lambda kv: -SEVERITY_RANK.get(kv[0], 0)))


def _package_line(package: str, rows: list[Row],
                  links: dict[str, str]) -> str:
    keys = sorted({r.key for r in rows},
                  key=lambda k: -max(SEVERITY_RANK.get(r.severity, 0)
                                     for r in rows if r.key == k))
    tally = _tally(rows)
    fixes = [r.fixed for r in rows if r.fixed]
    fix = f", fixed in {max(fixes, key=_vkey)}" if fixes else ", no fix yet"
    shown = ", ".join(f"<{links.get(k, '')}|{k}>" if links.get(k) else k
                      for k in keys[:2])
    more = f" and {len(keys) - 2} more" if len(keys) > 2 else ""
    # Name the CVE and the versions it hits. CVE-2023-4863 (libwebp, in
    # KEV) affects Pillow 10.0.0 but not 10.4.0; "Pillow is exploited"
    # would send someone to upgrade the wrong repo first.
    flag = ""
    for k in sorted({r.key for r in rows if r.kev}):
        flag += (f" *{k} is exploited in the wild (CISA KEV)*: "
                 f"{_repo_versions([r for r in rows if r.key == k])}.")
    likely_row = max(rows, key=lambda r: r.epss or 0)
    likely = ""
    if (likely_row.epss or 0) >= _epss_threshold():
        likely = (f" EPSS {likely_row.epss:.0%} for {likely_row.key} "
                  f"(odds of exploitation in 30 days).")
    return (f"*{package}* {_repo_versions(rows)}: {tally}{fix}. "
            f"{shown}{more}.{flag}{likely}")


def _group(rows: list[Row]) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = {}
    for r in rows:
        groups.setdefault(r.package, []).append(r)
    return dict(sorted(
        groups.items(),
        key=lambda kv: (
            -any(r.kev for r in kv[1]),
            -max(SEVERITY_RANK.get(r.severity, 0) for r in kv[1]),
            kv[0].lower(),
        ),
    ))


def _eol_line(e: Eol) -> str:
    name = EOL_NAMES.get(e.product, e.product)
    where = ", ".join(e.where)
    if e.status == "eol":
        since = f" since {e.eol_date}" if e.eol_date else ""
        return (f"*{name} {e.cycle}* ({where}): no security fixes{since}. "
                f"Upgrade to a supported release.")
    return (f"*{name} {e.cycle}* ({where}): support ends {e.eol_date}.")


def _task_name(r: Row) -> str:
    """How an alert line finds its subtask: the package, or the
    end-of-life cycle."""
    return r.key if r.key.startswith("EOL:") else r.package


def _owner_note(name: str, owners: list[str],
                tasks: dict[tuple[str, str], str]) -> str:
    parts = [f"<{tasks[(name, o)]}|{OWNER_NAMES[o]}'s task>"
             if tasks.get((name, o)) else OWNER_NAMES[o] for o in owners]
    return f" Owner: {', '.join(parts)}." if parts else ""


def _tags(owners: list[str]) -> str:
    """The owners tagged, Sam copied, for the top of an alert."""
    return " ".join([_mention(o) for o in owners]
                    + [f"cc {_mention('sam')}"])


def alert_message(rows: list[Row], links: dict[str, str],
                  eols: list[Eol],
                  tasks: dict[tuple[str, str], str]) -> str:
    vuln = [r for r in rows if not r.key.startswith("EOL:")]
    owners = _owners([r.repo for r in vuln]
                     + [w for e in eols for w in e.where])
    lines = [f"{_tags(owners)} :rotating_light: *Dependency alert*: "
             + ("something new needs a look today."
                if len(_group(vuln)) + len(eols) == 1
                else f"{len(_group(vuln)) + len(eols)} new things need a "
                     "look today.")]
    for package, group in _group(vuln).items():
        lines.append("• " + _package_line(package, group, links)
                     + _owner_note(package, _owners(r.repo for r in group),
                                   tasks))
    for e in eols:
        lines.append("• " + _eol_line(e)
                     + _owner_note(e.key, _owners(e.where), tasks))
    lines.append("_Daily dependency watch. Each of these is said once; the "
                 "weekly digest keeps the full list._")
    return "\n".join(lines)


def digest_messages(rows: list[Row], links: dict[str, str], eols: list[Eol],
                    problems: list[str], inv: Inventory, resolved: int,
                    news: list[dict] | None, baseline: bool = False,
                    today: date | None = None) -> tuple[str, list[str]]:
    """(main message, thread replies)."""
    today = today or date.today()
    vuln = [r for r in rows if not r.key.startswith("EOL:")]
    act = [r for r in vuln if r.tier in (KEV, URGENT)]
    rest = [r for r in vuln if r.tier == DIGEST]
    title = ("first scan, everything we run today" if baseline
             else f"week of {today:%b} {today.day}")
    versions = {(p.ecosystem, p.name, p.version) for p in inv.packages}
    lines = [f":shield: *Dependency watch*, {title}",
             f"Read {len(inv.scanned)} repos at their live branches, "
             f"{len(versions):,} package versions."]

    groups = _group(act)
    lines.append("")
    if groups:
        lines.append(f"*Upgrade soon* ({len(groups)} packages, exploited, "
                     "critical, or high on a package we lean on):")
        for package, group in list(groups.items())[:12]:
            lines.append("• " + _package_line(package, group, links))
        if len(groups) > 12:
            lines.append(f"• and {len(groups) - 12} more, in the thread.")
    else:
        lines.append("*Upgrade soon*: nothing.")

    bad = [e for e in eols if e.status in ("eol", "soon")]
    if bad:
        lines += ["", "*End of life*:"]
        lines += ["• " + _eol_line(e) for e in
                  sorted(bad, key=lambda e: (e.status != "eol", e.product))]
    unlisted = [e for e in eols if e.status == "unlisted"]

    not_checked = list(problems)
    not_checked += [f"{EOL_NAMES.get(e.product, e.product)} {e.version} "
                    f"({', '.join(e.where)}): not on endoflife.date"
                    for e in unlisted]
    not_checked += [f"{_label(repo)}: {n} unpinned requirement(s), not "
                    "matched" for repo, n in sorted(inv.unpinned.items())]
    not_checked.append("salesforce: the managed package has no advisory "
                       "feed. Check <https://trust.salesforce.com|Salesforce "
                       "Trust> by hand.")
    lines += ["", "*Not checked*:"] + [f"• {p}" for p in not_checked]

    if news:
        lines += ["", "*In the news* (a heads-up, not a finding):"]
        for h in news[:5]:
            target = h["url"] or h["hn"]
            lines.append(f"• <{target}|{h['title']}> "
                         f"(<{h['hn']}|HN>, {h['points']} points)")

    tail = []
    if resolved:
        tail.append(f"{resolved} resolved since the last digest")
    if rest:
        tail.append(f"{len(_group(rest))} more packages with moderate, "
                    "build-only or unfixed advisories, listed in the thread")
    if tail:
        lines += ["", "_" + ". ".join(tail).capitalize() + "._"]

    thread = []
    overflow = list(groups.items())[12:]
    rest_groups = list(_group(rest).items())
    if overflow or rest_groups:
        chunk = ["*Everything else*, by package:"]
        for package, group in overflow + rest_groups:
            line = "• " + _package_line(package, group, links)
            if sum(len(c) + 1 for c in chunk) + len(line) > 3500:
                thread.append("\n".join(chunk))
                chunk = []
                if len(thread) == 5:
                    break
            chunk.append(line)
        else:
            thread.append("\n".join(chunk))
        if len(thread) == 5:
            thread[-1] += "\n_Cut here; run `py dependency_watch.py` for all._"
    return "\n".join(lines), thread


# ---------------------------------------------------------------------------
# Claude's note
# ---------------------------------------------------------------------------

_REVIEW_PROMPT = """You are reviewing dependency vulnerability alerts for \
Vome's engineers. Vome is a volunteer management platform: four Django \
backends (core, database, chats, integrations), a React web app, an Astro \
landing page and a React Native mobile app.

Code has already matched these advisories to the versions Vome runs and \
decided they need a look. Do not second-guess that or re-rank them. For each \
package below, write one Slack bullet of at most two short sentences: what \
the flaw lets an attacker do, what kind of input reaches it (for example an \
uploaded image, a URL, a token, a build step), and the first thing to do.

Rules:
- Use only the facts given. Never invent a CVE, version, exploit or fix.
- If the text does not say how it is reached, say the advisory does not say.
- Plain words, no headings, no preamble, no closing line.
- No em dashes or en dashes. Use commas, periods or parentheses.
- Format each bullet as: • *package*: note

{packages}"""


def _ask_claude(prompt: str) -> str:
    """Claude's answer, or "" when it is switched off or does not answer."""
    if not _flag("DEPENDENCY_WATCH_CLAUDE"):
        return ""
    try:
        import anthropic

        from model_config import SUPPORT_MODEL_FAST

        resp = anthropic.Anthropic().messages.create(
            model=SUPPORT_MODEL_FAST, max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"[DEPWATCH] Claude note skipped: {exc}")
        return ""


def claude_note(rows: list[Row], details: dict[str, str],
                limit: int = 8) -> str:
    """Claude's read of what was flagged, as a thread reply, or "". Reads
    rows the code already tiered; it changes nothing about who is told."""
    groups = list(_group([r for r in rows
                          if not r.key.startswith("EOL:")]).items())[:limit]
    if not groups:
        return ""
    blocks = []
    for package, group in groups:
        block = [f"PACKAGE {package}: " + _package_line(package, group, {})]
        for key in sorted({r.key for r in group}):
            summary = next((r.summary for r in group if r.key == key), "")
            block.append(f"  {key}: {summary}")
            if details.get(key):
                block.append("    " + details[key][:800].replace("\n", " "))
        blocks.append("\n".join(block))
    text = _ask_claude(_REVIEW_PROMPT.format(packages="\n\n".join(blocks)))
    if not text:
        return ""
    text = text.replace("\N{EM DASH}", ", ").replace("\N{EN DASH}", "-")
    return ("*Claude's read* (from the advisory text; the code above decided "
            "what to flag):\n" + text)


# ---------------------------------------------------------------------------
# Slack and ClickUp
# ---------------------------------------------------------------------------

def _post(text: str, thread_ts: str = "") -> str:
    """The message ts, or "" when it did not post."""
    if not CHANNEL:
        print("[DEPWATCH] SLACK_CHANNEL_ENG_INFRA not set, not posting")
        return ""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        kwargs = {"channel": CHANNEL, "text": text, "unfurl_links": False,
                  "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return client.chat_postMessage(**kwargs).get("ts") or ""
    except SlackApiError as exc:
        print(f"[DEPWATCH] Slack post failed: {exc.response['error']}")
    except Exception as exc:
        print(f"[DEPWATCH] Slack post failed: {exc}")
    return ""


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@dataclass
class Scan:
    inv: Inventory
    findings: list[Finding]
    eols: list[Eol]
    problems: list[str]
    rows: list[Row]
    links: dict[str, str]
    details: dict[str, str] = field(default_factory=dict)


def scan(repos=None) -> Scan:
    inv = build_inventory(repos)
    findings, problems = match(inv) if inv.packages else ([], [])
    eols, eol_problems = check_eol(inv)
    watchlist = _watchlist()
    rows = [_row(f, watchlist) for f in findings] + _eol_rows(eols)
    links = {f.key: f.link for f in findings}
    details = {f.key: f.details for f in findings if f.details}
    return Scan(inv, findings, eols, inv.problems + problems + eol_problems,
                rows, links, details)


# ---------------------------------------------------------------------------
# The upgrade plan: one task per engineer, one subtask per upgrade
# ---------------------------------------------------------------------------

PARENT_TITLES = {"onlyg": "[Security] Backend dependency upgrades",
                 "sanjay": "[Security] Frontend dependency upgrades"}
OWNER_AREAS = {"onlyg": "core, database, chats, integrations and the servers",
               "sanjay": "the web app, landing page, mobile app and the web "
                         "build"}
PARENT, QUICK = "*", "quick"
CLOSED = "Closed"
CHECKLIST = "Upgrades"

# Packages one upgrade fixes together: one subtask, named after the first.
FAMILIES = {"pdfjs-dist": "react-pdf", "pyopenssl": "cryptography"}
FAMILY_NOTES = {
    "react-pdf": "react-pdf bundles pdfjs-dist, so one upgrade clears both "
                 "once the new react-pdf brings pdfjs-dist 4.2.67 or later. "
                 "Check which pdfjs-dist it pulls in.",
    "cryptography": "pyOpenSSL pins the cryptography versions it accepts, so "
                    "upgrade the two together.",
}
# A major upgrade of one of these is a project, not a bump.
FRAMEWORKS = {"django", "nodejs", "python", "react", "react-native", "expo",
              "astro", "postgresql", "redis"}
# Build and install tools: they never run in production.
BUILD_TOOLS = {"virtualenv", "pip", "pipenv", "tox", "nox"}
# Numbered by year, so a new first number is not a breaking change.
CALVER = {"certifi", "twisted"}

LEVEL_WORDS = {"critical": "Critical", "high": "High", "low": "Low"}
PRIORITY = {"critical": 1, "high": 2, "low": 3}
SIZE_RANK = {"patch": 0, "minor": 1, "major": 2, "project": 3, "nofix": 4}
SIZE_WORDS = {"patch": "Patch", "minor": "Minor", "major": "Major upgrade",
              "project": "Project", "nofix": "Replace or wait"}
SIZE_NOTES = {
    "patch": "Patch. A drop-in upgrade.",
    "minor": "Minor. Should be drop-in; skim the changelog.",
    "major": "Major upgrade. A new major version can break things: read the "
             "release notes for breaking changes first.",
    "project": "Project. A major framework or runtime upgrade: read the "
               "release notes, plan a full test pass, and ship it to staging "
               "first.",
    "nofix": "No fixed release yet. Watch the advisory, or replace the "
             "package.",
}
SECTIONS = (
    ("critical", "**Critical** (exploited in the wild, or likely to be soon): "
                 "do these first"),
    ("project", "**Projects** (framework or runtime upgrades): plan these"),
    ("major", "**Major upgrades**: read the release notes first"),
    ("nofix", "**No fixed release yet**: watch, or replace the package"),
    ("quick", "**Quick upgrades**: patch and minor bumps, one checklist"),
)
FOOTER = ("Kept up to date by the VomeOS dependency watch "
          "(dependency_watch.py), which reads each repo's lockfile on its "
          "live branch every morning and closes this once the upgrade is "
          "there.")


def _unit_of(r: Row) -> str:
    name = re.sub(r"[-_.]+", "-", r.package).lower()
    return FAMILIES.get(name, name)


def plan_units(rows: list[Row]) -> dict[tuple[str, str], list[Row]]:
    """(owner, upgrade) -> rows. An upgrade is a package with its family and
    its end-of-life cycle: Django's advisories and Django 4.2 being out of
    support are one move to 5.2. Only upgrades with something at alert
    level, but with every row they have, so the target also clears what
    only the digest lists (astro 7.3.2, not the 7.2.8 the critical one
    needs)."""
    out: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        out.setdefault((owner_of(r.repo), _unit_of(r)), []).append(r)
    return {k: v for k, v in out.items()
            if any(r.tier in (KEV, URGENT) for r in v)}


def _display(r: Row) -> str:
    if r.key.startswith("EOL:"):
        return EOL_NAMES.get(r.package, r.package)
    return r.package


def _moves(unit: str, rows: list[Row]) -> dict[str, tuple[list[str], str]]:
    """Display name -> (versions we run, version to move to), the upgrade's
    own package first. The target clears every advisory; an end-of-life
    cycle gives one only when no advisory does."""
    names = sorted(dict.fromkeys(_display(r) for r in rows),
                   key=lambda n: n.lower() != unit)
    out = {}
    for name in names:
        mine = [r for r in rows if _display(r) == name]
        fixes = [r.fixed for r in mine
                 if r.fixed and not r.key.startswith("EOL:")]
        cycles = [r.fixed for r in mine
                  if r.fixed and r.key.startswith("EOL:")]
        target = max(fixes, key=_vkey) if fixes else \
            (cycles[0] if cycles else "")
        out[name] = (sorted({r.version for r in mine}, key=_vkey), target)
    return out


def _span(versions: list[str]) -> str:
    if len(versions) > 2:
        return f"{versions[0]} to {versions[-1]}"
    return ", ".join(versions)


def _target_text(target: str) -> str:
    if _vkey(target)[-1] == 0:
        return f"{target} (release candidate)"
    return target


def _move_text(unit: str, rows: list[Row]) -> str:
    """"Pillow 10.0.0, 10.4.0 → 13.0.0"."""
    return ", ".join(
        f"{name} {_span(versions)} → {_target_text(target)}" if target
        else f"{name} {_span(versions)} (no fix yet)"
        for name, (versions, target) in _moves(unit, rows).items())


def _bump(name: str, current: str, target: str) -> str:
    a, b = _vkey(current), _vkey(target)
    if b[:5] <= a[:5]:
        return "patch"
    if name.lower() in CALVER or a[0] >= 2000:
        return "minor"
    if a[0] != b[0]:
        return "major"
    return "minor" if a[1] != b[1] else "patch"


def _size(unit: str, rows: list[Row]) -> str:
    """How big the job is, from the version numbers alone."""
    worst = "patch"
    for name, (versions, target) in _moves(unit, rows).items():
        eol = all(r.key.startswith("EOL:") for r in rows
                  if _display(r) == name)
        for v in versions:
            if target:
                size = _bump(name, v, target)
            else:
                size = "major" if eol else "nofix"
            if SIZE_RANK[size] > SIZE_RANK[worst]:
                worst = size
    if worst == "major" and unit in FRAMEWORKS:
        return "project"
    return worst


def _level(unit: str, rows: list[Row]) -> str:
    """How urgent: exploited or likely to be, a build tool, or neither.
    Read from what alerted: a likely exploit in a devDependency is not."""
    alerting = [r for r in rows if r.tier in (KEV, URGENT)] or rows
    if any(r.kev or (r.epss or 0) >= _epss_threshold() for r in alerting):
        return "critical"
    return "low" if unit in BUILD_TOOLS else "high"


def _slot(unit: str, rows: list[Row]) -> str:
    """Its own subtask, or a line on the owner's quick-upgrades checklist."""
    level = _level(unit, rows)
    if level == "critical" or (level == "high" and _size(unit, rows) in (
            "major", "project", "nofix")):
        return unit
    return QUICK


def _section(slot: str, units: dict[str, list[Row]]) -> str:
    if slot == QUICK:
        return "quick"
    rows = units[slot]
    if _level(slot, rows) == "critical":
        return "critical"
    return _size(slot, rows)


def _why_level(unit: str, rows: list[Row]) -> str:
    kev = sorted({r.key for r in rows if r.kev})
    if kev:
        return (f"Critical. {', '.join(kev)} "
                f"{'is' if len(kev) == 1 else 'are'} exploited in the wild "
                "(CISA KEV).")
    level = _level(unit, rows)
    if level == "critical":
        r = max((r for r in rows if r.tier in (KEV, URGENT)),
                key=lambda r: r.epss or 0)
        return (f"Critical. EPSS gives {r.key} {r.epss:.0%} odds of being "
                "exploited in the next 30 days.")
    if level == "low":
        return "Low. A build tool: it never runs in production."
    vuln = [r for r in rows if not r.key.startswith("EOL:")]
    parts = [f"advisories in code we ship ({_tally(vuln)})"] if vuln else []
    for key in dict.fromkeys(r.key for r in rows
                             if r.key.startswith("EOL:")):
        r = next(r for r in rows if r.key == key)
        parts.append(f"{_display(r)} {key.rsplit(':', 1)[-1]} is out of "
                     f"support ({r.summary})")
    text = ", and ".join(parts)
    return f"High. {text[:1].upper()}{text[1:]}."


def _package_section(name: str, rows: list[Row], target: str,
                     links: dict[str, str]) -> list[str]:
    vuln = [r for r in rows if not r.key.startswith("EOL:")]
    out = [f"### {name}", f"**Where:** {_repo_versions(rows)}",
           "**Upgrade to:** " + (_target_text(target) if target
                                 else "no fixed release yet")
           + (" (clears every advisory below)" if target and vuln else "")]
    for key in dict.fromkeys(r.key for r in rows
                             if r.key.startswith("EOL:")):
        r = next(r for r in rows if r.key == key)
        out.append(f"**Out of support:** {name} {key.rsplit(':', 1)[-1]} "
                   f"gets no more security fixes ({r.summary}). See "
                   f"https://endoflife.date/{r.package}")
    kev = sorted({r.key for r in vuln if r.kev})
    out += [f"**Exploited in the wild (CISA KEV):** {k} on "
            f"{_repo_versions([r for r in vuln if r.key == k])}" for k in kev]
    if not vuln:
        return out
    keys = sorted({r.key for r in vuln},
                  key=lambda k: (k not in kev, -max(
                      SEVERITY_RANK.get(r.severity, 0)
                      for r in vuln if r.key == k)))
    out += ["", f"**Advisories** ({_tally(vuln)})"]
    for k in keys[:20]:
        r = next(r for r in vuln if r.key == k)
        link = f"[{k}]({links[k]})" if links.get(k) else k
        out.append(f"- {link} ({r.severity.lower()}): {r.summary}")
    if len(keys) > 20:
        out.append(f"- and {len(keys) - 20} more, in the weekly digest")
    return out


def subtask_title(unit: str, rows: list[Row]) -> str:
    """How urgent · the version we run → the one to move to · how big."""
    flag = " (exploited)" if any(r.kev for r in rows) else ""
    return (f"{LEVEL_WORDS[_level(unit, rows)]}{flag} · "
            f"{_move_text(unit, rows)} · "
            f"{SIZE_WORDS[_size(unit, rows)]}")[:180]


def subtask_body(unit: str, rows: list[Row], links: dict[str, str]) -> str:
    owner = owner_of(rows[0].repo)
    body = [f"**How urgent:** {_why_level(unit, rows)}",
            f"**How big:** {SIZE_NOTES[_size(unit, rows)]}",
            f"**Owner:** {OWNER_NAMES[owner]}", ""]
    if FAMILY_NOTES.get(unit):
        body += [FAMILY_NOTES[unit], ""]
    for name, (_, target) in _moves(unit, rows).items():
        body += _package_section(
            name, [r for r in rows if _display(r) == name], target,
            links) + [""]
    return "\n".join(body + [FOOTER])


def quick_items(units: dict[str, list[Row]]) -> list[tuple[str, str]]:
    """(match prefix, checklist line) per upgrade on the quick checklist.
    A line is found again by its package name, so a new version number
    renames it instead of adding a second one."""
    out = []
    for unit, rows in sorted(units.items(), key=lambda kv: (
            _level(*kv) == "low", kv[0])):
        head = next(iter(_moves(unit, rows)))
        where = ", ".join(sorted({_label(r.repo) for r in rows}))
        tool = " · build tool" if _level(unit, rows) == "low" else ""
        out.append((head, f"{_move_text(unit, rows)} ({where}) · "
                          f"{SIZE_WORDS[_size(unit, rows)]}{tool}"))
    return out


def quick_title(units: dict[str, list[Row]]) -> str:
    level = "high" if any(_level(*kv) == "high" for kv in units.items()) \
        else "low"
    n = len(units)
    return (f"{LEVEL_WORDS[level]} · Quick upgrades · {n} "
            f"package{'s' if n != 1 else ''}")


def quick_body(units: dict[str, list[Row]], owner: str,
               links: dict[str, str]) -> str:
    body = ["**How urgent:** High: each has a critical or high advisory in "
            "code we ship. Build tools are Low: they never run in "
            "production.",
            "**How big:** Patch and minor upgrades (and build tools): each "
            "should be drop-in. The checklist ticks itself when the live "
            "branch shows the upgrade.",
            f"**Owner:** {OWNER_NAMES[owner]}", ""]
    for unit, rows in sorted(units.items()):
        vuln = [r for r in rows if not r.key.startswith("EOL:")]
        keys = sorted({r.key for r in vuln}, key=lambda k: -max(
            SEVERITY_RANK.get(r.severity, 0) for r in vuln if r.key == k))
        shown = ", ".join(f"[{k}]({links[k]})" if links.get(k) else k
                          for k in keys[:3])
        more = f" and {len(keys) - 3} more" if len(keys) > 3 else ""
        body.append(f"- **{_move_text(unit, rows)}** "
                    f"({_repo_versions(rows)}) · "
                    f"{SIZE_WORDS[_size(unit, rows)]} · {_tally(vuln)}: "
                    f"{shown}{more}")
    return "\n".join(body + ["", FOOTER])


def _fingerprint(*parts) -> str:
    import hashlib

    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:16]


def _complete(s: "Scan", owner: str) -> bool:
    """Whether this run saw everything of `owner`'s: every repo read and
    every feed answered. Only then is a missing finding fixed rather than
    unseen, so only then does anything close or get relabelled."""
    repos = {slug for slug in MANIFESTS if owner_of(slug) == owner}
    if not repos <= s.inv.scanned:
        return False
    return not any(p.startswith(("OSV.dev", "CISA KEV", "FIRST EPSS",
                                 "endoflife.date", "NVD"))
                   for p in s.problems)


@dataclass
class Plan:
    parents: dict[str, str] = field(default_factory=dict)  # owner -> id
    # (owner, slot) -> {"id", "title", "section", "count"}
    slots: dict[tuple[str, str], dict] = field(default_factory=dict)
    # (package or EOL key, owner) -> subtask URL, for the alert's links
    links: dict[tuple[str, str], str] = field(default_factory=dict)
    created: list[tuple[str, str]] = field(default_factory=list)
    closed: list[tuple[str, str]] = field(default_factory=list)
    moved: int = 0


def _url(task_id: str) -> str:
    return f"https://app.clickup.com/t/{task_id}"


def _clickup(method: str, path: str, body: dict | None = None) -> dict:
    resp = httpx.request(
        method, f"{CLICKUP_BASE}{path}",
        headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", ""),
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _new_task(name: str, owner: str, body: str, priority: int,
              parent: str = "") -> str:
    """A task in the Priority Queue assigned to `owner`, or a subtask of
    `parent`. Returns its id, or "" when ClickUp said no."""
    from sentry_agent_fixes import (
        FIELD_SOURCE,
        FIELD_TYPE,
        SOURCE_INTERNAL,
        TYPE_BUG,
    )

    assignee = _clickup_user(owner)
    payload = {
        "name": name[:180], "markdown_description": body,
        "assignees": [assignee] if assignee else [], "status": "queued",
        "priority": priority,
        "custom_fields": [{"id": FIELD_TYPE, "value": TYPE_BUG},
                          {"id": FIELD_SOURCE, "value": SOURCE_INTERNAL}],
    }
    if parent:
        payload["parent"] = parent
    try:
        return str(_clickup("POST", f"/list/{CLICKUP_LIST}/task",
                            payload).get("id") or "")
    except Exception as exc:
        print(f"[DEPWATCH] ClickUp task {name[:60]!r} failed: {exc}")
        return ""


def _update(task_id: str, payload: dict) -> bool:
    try:
        _clickup("PUT", f"/task/{task_id}", payload)
        return True
    except Exception as exc:
        print(f"[DEPWATCH] ClickUp update of {task_id} failed: {exc}")
        return False


def _close(task_id: str, comment: str) -> bool:
    try:
        _clickup("POST", f"/task/{task_id}/comment",
                 {"comment_text": comment, "notify_all": False})
        _clickup("PUT", f"/task/{task_id}", {"status": CLOSED})
        return True
    except Exception as exc:
        print(f"[DEPWATCH] closing ClickUp task {task_id} failed: {exc}")
        return False


def _sync_checklist(task_id: str, items: list[tuple[str, str]]) -> bool:
    """The quick subtask's checklist matches `items`: new lines added, moved
    versions renamed, and lines no longer open ticked."""
    try:
        task = _clickup("GET", f"/task/{task_id}")
        checklist = next((c for c in task.get("checklists") or []
                          if c.get("name") == CHECKLIST), None)
        if checklist is None:
            checklist = _clickup("POST", f"/task/{task_id}/checklist",
                                 {"name": CHECKLIST}).get("checklist") or {}
        cid = checklist["id"]
        existing = checklist.get("items") or []
        for prefix, text in items:
            item = next((i for i in existing
                         if str(i.get("name", "")).startswith(prefix + " ")),
                        None)
            if item is None:
                _clickup("POST", f"/checklist/{cid}/checklist_item",
                         {"name": text})
            elif item.get("name") != text:
                _clickup("PUT", f"/checklist/{cid}/checklist_item/"
                                f"{item['id']}", {"name": text})
        for item in existing:
            name = str(item.get("name", ""))
            if not item.get("resolved") and not any(
                    name.startswith(p + " ") for p, _ in items):
                _clickup("PUT", f"/checklist/{cid}/checklist_item/"
                                f"{item['id']}", {"resolved": True})
        return True
    except Exception as exc:
        print(f"[DEPWATCH] checklist on {task_id} failed: {exc}")
        return False


def _render(slot: str, units: dict[str, list[Row]], owner: str,
            links: dict[str, str]) -> tuple[str, str, list, str]:
    """(title, description, checklist, level) for one subtask."""
    if slot == QUICK:
        level = "high" if any(_level(*kv) == "high"
                              for kv in units.items()) else "low"
        return (quick_title(units), quick_body(units, owner, links),
                quick_items(units), level)
    rows = units[slot]
    return (subtask_title(slot, rows), subtask_body(slot, rows, links), [],
            _level(slot, rows))


def parent_body(owner: str, plan: Plan) -> str:
    lines = [f"Every dependency upgrade the VomeOS dependency watch has open "
             f"in {OWNER_AREAS[owner]}. One subtask per upgrade, named how "
             "urgent · the version we run → the one to move to · how big. "
             "The watch rewrites this list every morning and closes a "
             "subtask once the live branch shows the upgrade.", ""]
    mine = sorted(((v["title"], v) for (o, _), v in plan.slots.items()
                   if o == owner), key=lambda tv: tv[0].lower())
    for section, heading in SECTIONS:
        rows = [f"- [{t}]({_url(v['id'])})" for t, v in mine
                if v["section"] == section]
        if rows:
            lines += [heading] + rows + [""]
    lines += [
        "**How the labels are decided** (in code, from OSV.dev, CISA KEV, "
        "FIRST EPSS and endoflife.date):",
        f"- Critical: exploited in the wild (CISA KEV), or EPSS at or above "
        f"{_epss_threshold():.0%}. High: a critical or high advisory in code "
        "we ship, or out of support. Low: a build tool that never runs in "
        "production.",
        "- Patch and Minor: drop-in. Major upgrade: a new major version. "
        "Project: a major upgrade of a framework or runtime (Django, "
        "Node.js, Python, React, React Native, Astro).",
    ]
    return "\n".join(lines)


def sync_plan(s: "Scan", ledger, told: dict) -> Plan:
    """Bring each engineer's upgrade task in line with this morning's scan:
    a subtask for every upgrade at alert level, relabelled when the facts
    change, closed when the live branch shows the fix."""
    plan = Plan()
    if not os.environ.get("CLICKUP_API_TOKEN"):
        return plan
    units = plan_units(s.rows)
    slots: dict[tuple[str, str], dict[str, list[Row]]] = {}
    for (owner, unit), rows in units.items():
        slots.setdefault((owner, _slot(unit, rows)), {})[unit] = rows
    marks = ledger.plan()
    for owner in OWNER_NAMES:
        _sync_owner(owner, {k: v for k, v in slots.items() if k[0] == owner},
                    marks, s, ledger, plan)
    _retire_old_tasks(slots, told, s, ledger, plan)
    return plan


def _sync_owner(owner: str, mine: dict, marks: dict, s: "Scan", ledger,
                plan: Plan) -> None:
    complete = _complete(s, owner)
    if complete:
        for (o, slot), mark in list(marks.items()):
            if o != owner or slot == PARENT or (o, slot) in mine:
                continue
            if _close(mark["clickup_task_id"],
                      "Done: the live branch now runs a release that clears "
                      "everything this listed."):
                ledger.drop_plan(o, slot)
                plan.closed.append((o, slot))
    parent = (marks.get((owner, PARENT)) or {}).get("clickup_task_id") or ""
    if not mine:
        if parent and complete and _close(
                parent, "Nothing open: every upgrade on this list is on the "
                        "live branch."):
            ledger.drop_plan(owner, PARENT)
        return
    if not parent:
        parent = _new_task(PARENT_TITLES[owner], owner,
                           "Filling in the upgrades.", 2)
        if not parent:
            return
        ledger.set_plan(owner, PARENT, parent, "")
        marks[(owner, PARENT)] = {"clickup_task_id": parent, "summary": ""}
    plan.parents[owner] = parent

    levels = []
    for (_, slot), units in sorted(mine.items()):
        title, body, items, level = _render(slot, units, owner, s.links)
        fp = _fingerprint(title, body, items)
        mark = marks.get((owner, slot)) or {}
        tid = mark.get("clickup_task_id") or ""
        if not tid:
            tid = _new_task(title, owner, body, PRIORITY[level], parent)
            if not tid:
                continue
            if items:
                _sync_checklist(tid, items)
            ledger.set_plan(owner, slot, tid, fp)
            plan.created.append((owner, slot))
        elif mark.get("summary") != fp and complete:
            if _update(tid, {"name": title, "markdown_content": body,
                             "priority": PRIORITY[level]}) and \
                    (not items or _sync_checklist(tid, items)):
                ledger.set_plan(owner, slot, tid, fp)
        levels.append(level)
        plan.slots[(owner, slot)] = {
            "id": tid, "title": title, "section": _section(slot, units),
            "count": len(units)}
        for rows in units.values():
            for r in rows:
                plan.links[(_task_name(r), owner)] = _url(tid)

    body = parent_body(owner, plan)
    priority = min((PRIORITY[lv] for lv in levels), default=2)
    fp = _fingerprint(body, priority)
    if marks[(owner, PARENT)].get("summary") != fp and _update(
            parent, {"name": PARENT_TITLES[owner], "markdown_content": body,
                     "priority": priority}):
        ledger.set_plan(owner, PARENT, parent, fp)


def _retire_old_tasks(slots: dict, told: dict, s: "Scan", ledger,
                      plan: Plan) -> None:
    """Tasks from before the plan (one per package, Oct 25 and 26): each is
    closed with a link to where its upgrade went, and the ledger points at
    the subtask instead."""
    current = {m.get("clickup_task_id") for m in ledger.plan().values()}
    home: dict[tuple[str, str, str], tuple[str, str]] = {}
    for key, units in slots.items():
        for rows in units.values():
            for r in rows:
                home[r.ident] = key
    old: dict[str, list[tuple[str, str, str]]] = {}
    for ident, row in told.items():
        tid = row.get("clickup_task_id")
        if tid and tid not in current and ident[0] not in ("PLAN",
                                                           "BASELINE"):
            old.setdefault(tid, []).append(ident)

    kept = set()
    for tid, idents in old.items():
        key = next((home[i] for i in idents if home.get(i) in plan.slots),
                   None)
        if key:
            slot = plan.slots[key]
            note = (f"Moved: this upgrade is now the subtask "
                    f"\"{slot['title']}\" ({_url(slot['id'])}) under "
                    f"{_url(plan.parents[key[0]])}. The dependency watch "
                    "keeps one task per engineer with a subtask per "
                    "upgrade, so this task is closed.")
        elif all(_complete(s, owner_of(i[1])) for i in idents):
            note = ("Closed: nothing on this task is at alert level any "
                    "more. The weekly digest still lists anything open.")
        else:
            kept.add(tid)
            continue
        if _close(tid, note):
            ledger.clear_task(tid)
            plan.moved += 1
        else:
            kept.add(tid)

    for key, units in slots.items():
        if key not in plan.slots:
            continue
        tid = plan.slots[key]["id"]
        stale = [r for rows in units.values() for r in rows
                 if (told.get(r.ident) or {}).get("clickup_task_id")
                 not in kept | {tid}]
        ledger.set_task(stale, tid)


def _parent_links(plan: Plan) -> str:
    """"<url|Backend dependency upgrades> for @OnlyG, ... cc @Sam"."""
    return ", ".join(
        f"<{_url(tid)}|{PARENT_TITLES[o].removeprefix('[Security] ')}> for "
        f"{_mention(o)}" for o, tid in plan.parents.items()
    ) + f", cc {_mention('sam')}"


def _plan_counts(plan: Plan, owner: str) -> str:
    """"1 critical, 1 project, 5 major upgrades, 6 quick upgrades"."""
    counts: dict[str, int] = {}
    for (o, _), v in plan.slots.items():
        if o == owner:
            counts[v["section"]] = counts.get(v["section"], 0) + v["count"]
    words = {"critical": ("critical", "critical"),
             "project": ("project", "projects"),
             "major": ("major upgrade", "major upgrades"),
             "nofix": ("with no fix yet", "with no fix yet"),
             "quick": ("quick upgrade on one checklist",
                       "quick upgrades on one checklist")}
    return ", ".join(f"{counts[s]} {words[s][counts[s] != 1]}"
                     for s, _ in SECTIONS if counts.get(s))


def _plan_list(plan: Plan) -> str:
    lines = []
    for owner, tid in plan.parents.items():
        lines.append(f"*<{_url(tid)}|{PARENT_TITLES[owner]}>* "
                     f"({OWNER_NAMES[owner]}): {_plan_counts(plan, owner)}")
        lines += [f"• <{_url(v['id'])}|{v['title']}>"
                  for (o, _), v in sorted(plan.slots.items()) if o == owner]
    return "\n".join(lines)


def plan_news(plan: Plan, due: list[Row]) -> str:
    """What the morning's alert does not already say: the old tasks moving
    into the plan, or a subtask opened for something that did not alert."""
    if plan.moved:
        n = plan.moved
        lines = [f"{_tags(list(plan.parents))} :clipboard: *Dependency "
                 "upgrades* are now one task per engineer, with a subtask "
                 "per upgrade:"]
        lines += [f"• <{_url(tid)}|"
                  f"{PARENT_TITLES[o].removeprefix('[Security] ')}> "
                  f"({OWNER_NAMES[o]}): {_plan_counts(plan, o)}"
                  for o, tid in plan.parents.items()]
        lines.append(f"Each subtask says how urgent it is, the version we "
                     f"run → the one to move to, and how big the job is. The "
                     f"{n} earlier task{'s are' if n != 1 else ' is'} closed, "
                     "each with a link to its new place.")
        return "\n".join(lines)
    alerted = {plan.links.get((_task_name(r), owner_of(r.repo))) for r in due}
    quiet = [k for k in plan.created
             if _url(plan.slots[k]["id"]) not in alerted]
    if not quiet:
        return ""
    owners = [o for o in OWNER_NAMES if any(k[0] == o for k in quiet)]
    return (f"{_tags(owners)} :clipboard: *Dependency upgrades*: "
            f"{len(quiet)} new subtask{'s' if len(quiet) != 1 else ''}.\n"
            + "\n".join(f"• <{_url(plan.slots[k]['id'])}|"
                        f"{plan.slots[k]['title']}>" for k in quiet))


def _log_failure(summary: str, details: str) -> None:
    try:
        import workflow_log

        workflow_log.log_issue("dependency watch", summary, summary, details)
    except Exception as exc:
        print(f"[DEPWATCH] could not log the failure: {exc}")


def run_daily_scan(post: bool = True) -> dict:
    """Every morning. Silent unless something new needs a look."""
    if not _enabled():
        print("[DEPWATCH] DEPENDENCY_WATCH_ENABLED is false, skipping")
        return {"status": "disabled"}
    s = scan()
    if not s.inv.scanned:
        _log_failure("Dependency watch could not read any repository",
                     "\n".join(s.problems) or "no repos were readable")
        return {"status": "unreadable", "problems": s.problems}

    ledger = Ledger() if post else Ledger(enabled=False)
    told = ledger.told()
    first = not ledger.baseline_done()
    due = [r for r in s.rows if needs_alert(r, told.get(r.ident))]
    ledger.record(s.rows)
    resolved = ledger.resolve_missing(s.rows, s.inv.scanned)

    summary = {"status": "ok", "packages": len(s.inv.packages),
               "findings": len(s.findings), "due": len(due),
               "resolved": resolved, "problems": s.problems}
    if not post:
        return summary
    if not ledger.available:
        print("[DEPWATCH] no ledger, holding same-day alerts so they do not "
              "repeat every morning")
        return {**summary, "status": "no_ledger"}

    # Each engineer's upgrade task, before anything posts, so the alert can
    # link the subtask and a failed post never files anything twice.
    plan = sync_plan(s, ledger, told)

    if first:
        text, thread = digest_messages(
            s.rows, s.links, s.eols, s.problems, s.inv, 0, None,
            baseline=True)
        if plan.parents:
            text += (f"\n\n*Assigned*: {_parent_links(plan)}. One subtask "
                     "per upgrade, listed in the thread.")
        ts = _post(text)
        note = claude_note([r for r in s.rows if r.tier in (KEV, URGENT)],
                           s.details) if ts else ""
        replies = ([_plan_list(plan)] if plan.slots else []) + \
            ([note] if note else []) + thread
        for reply in replies if ts else []:
            _post(reply, ts)
        if ts:
            ledger.mark_alerted([r for r in s.rows
                                 if r.tier in (KEV, URGENT)])
            ledger.mark_baseline()
        return {**summary, "status": "baseline", "posted": bool(ts)}

    ts = ""
    if due:
        eol_keys = {r.key for r in due if r.key.startswith("EOL:")}
        eols = [e for e in s.eols if e.key in eol_keys]
        ts = _post(alert_message(due, s.links, eols, plan.links))
        if ts:
            ledger.mark_alerted(due)
            note = claude_note(due, s.details)
            if note:
                _post(note, ts)
    news = plan_news(plan, due)
    if news:
        _post(news)
    return {**summary, "posted": bool(ts), "created": plan.created,
            "closed": plan.closed, "moved": plan.moved}


def _digest_every() -> int:
    try:
        return max(1, int(os.environ.get(
            "DEPENDENCY_WATCH_DIGEST_EVERY_WEEKS", "1")))
    except ValueError:
        return 1


def _digest_week_due(today: date) -> bool:
    return today.isocalendar()[1] % _digest_every() == 0


def run_weekly_digest(post: bool = True) -> dict:
    """The full picture, weekly (or every other week)."""
    if not _enabled():
        print("[DEPWATCH] DEPENDENCY_WATCH_ENABLED is false, skipping")
        return {"status": "disabled"}
    today = date.today()
    if post and not _digest_week_due(today):
        return {"status": "off_week"}
    ledger = Ledger() if post else Ledger(enabled=False)
    if post and not ledger.baseline_done():
        # The daily scan owns the first run's baseline post.
        return run_daily_scan(post=True)
    s = scan()
    ledger.record(s.rows)
    ledger.resolve_missing(s.rows, s.inv.scanned)
    every = _digest_every()
    resolved = ledger.resolved_since(_now() - timedelta(weeks=every))
    news = hn_chatter(days=7 * every + 1)
    text, thread = digest_messages(s.rows, s.links, s.eols, s.problems,
                                   s.inv, resolved, news, today=today)
    note = claude_note([r for r in s.rows if r.tier in (KEV, URGENT)],
                       s.details)
    replies = ([note] if note else []) + thread
    if not post:
        print(text)
        for reply in replies:
            print("\n--- thread ---\n" + reply)
        return {"status": "printed"}
    ts = _post(text)
    for reply in replies if ts else []:
        _post(reply, ts)
    return {"status": "ok", "posted": bool(ts),
            "packages": len(s.inv.packages), "findings": len(s.findings)}


def _print_scan() -> None:
    s = scan()
    print(f"Read: {', '.join(sorted(_label(r) for r in s.inv.scanned))}")
    print(f"{len(s.inv.packages)} packages, {len(s.findings)} findings\n")
    for t in (KEV, URGENT, DIGEST):
        groups = _group([r for r in s.rows if r.tier == t
                         and not r.key.startswith("EOL:")])
        print(f"== {t}: {len(groups)} packages")
        for package, group in groups.items():
            print("  " + _package_line(package, group, {}))
    print("\n== end of life")
    for e in s.eols:
        print(f"  {e.status:9} {_eol_line(e)}")
    print("\n== not checked")
    for p in s.problems:
        print(f"  {p}")
    print("\n== upgrade plan (the subtasks it would keep)")
    slots: dict[tuple[str, str], dict[str, list[Row]]] = {}
    for (owner, unit), rows in plan_units(s.rows).items():
        slots.setdefault((owner, _slot(unit, rows)), {})[unit] = rows
    for (owner, slot), units in sorted(slots.items()):
        title, _, items, _ = _render(slot, units, owner, {})
        print(f"  {OWNER_NAMES[owner]:7} {title}")
        for _, line in items:
            print(f"            [ ] {line}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--digest", action="store_true",
                        help="print the digest as it would post")
    if parser.parse_args().digest:
        run_weekly_digest(post=False)
    else:
        _print_scan()
