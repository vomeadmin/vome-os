"""
test_code_mirror.py

The full-copy search that stands in for GitHub's code search when GitHub's
index of a repository is incomplete (vome-react, #9582). No network: the
branch-head lookup and the tarball are served from memory.
"""

import inspect
import io
import tarfile

import httpx
import pytest

from vomeos.integrations import code_mirror, code_search
from vomeos.integrations.base import IntegrationResult

TOP = "vomeadmin-vome-react-abc123"

FILES = {
    "src/views/MyVolunteerUserList.js":
        "const x = 1;\n<TableColumnResizing columnWidths={w} />\n",
    "src/utils/widths.js": "// columnWidths are kept per view\nresizing();\n",
    "src/i18n/fr.json": '{"views": "Vues"}\n',
    "node_modules/dx/index.js": "TableColumnResizing in a dependency\n",
    "build/static/main.js": "TableColumnResizing in build output\n",
    "src/assets/hero.png": "TableColumnResizing pretending to be an image\n",
}


def _tarball(files: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, text in files.items():
            body = text.encode()
            info = tarfile.TarInfo(f"{TOP}/{path}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


class _Head:
    def __init__(self, sha):
        self.text = sha

    def raise_for_status(self):
        pass


class _Download:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        for i in range(0, len(self.body), 7):  # awkward chunk sizes on purpose
            yield self.body[i:i + 7]


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Serve `state["sha"]` and `state["files"]`; count downloads."""
    state = {"sha": "a" * 40, "files": dict(FILES), "downloads": 0}
    monkeypatch.setattr(code_mirror, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(code_mirror, "_sha_cache", {})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Head(state["sha"]))

    def fake_stream(method, url, **kwargs):
        assert method == "GET"
        assert url.endswith(f"/tarball/{state['sha']}")
        state["downloads"] += 1
        return _Download(_tarball(state["files"]))

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return state


def _paths(result):
    return [item["path"] for item in result.data["items"]]


def test_phrase_search_skips_dependencies_build_output_and_images(served):
    result = code_mirror.search("vomeadmin", "vome-react",
                                "TableColumnResizing", {}, ref="master")
    assert result.ok
    assert _paths(result) == ["src/views/MyVolunteerUserList.js"]
    line = result.data["items"][0]["content_matches"][0]["lines"][0]
    assert line["line"] == 2
    assert result.data["mirror"] == {"ref": "master", "sha": "a" * 12}


def test_quoted_phrase_and_case_are_ignored(served):
    result = code_mirror.search("vomeadmin", "vome-react",
                                '"tablecolumnresizing"', {})
    assert _paths(result) == ["src/views/MyVolunteerUserList.js"]


def test_multi_word_query_falls_back_to_every_word(served):
    # No file holds the exact phrase; two hold both words.
    result = code_mirror.search("vomeadmin", "vome-react",
                                "columnWidths resizing", {})
    assert sorted(_paths(result)) == [
        "src/utils/widths.js", "src/views/MyVolunteerUserList.js",
    ]


def test_json_translations_are_searchable(served):
    result = code_mirror.search("vomeadmin", "vome-react", "Vues", {})
    assert _paths(result) == ["src/i18n/fr.json"]


def test_same_commit_is_downloaded_once_and_a_new_one_replaces_it(
        served, tmp_path):
    code_mirror.search("vomeadmin", "vome-react", "views", {})
    code_mirror.search("vomeadmin", "vome-react", "views", {})
    assert served["downloads"] == 1

    served["sha"] = "b" * 40
    served["files"]["src/new.js"] = "brandNewSymbol\n"
    code_mirror._sha_cache.clear()  # as if the one-minute reuse had expired
    result = code_mirror.search("vomeadmin", "vome-react",
                                "brandNewSymbol", {})
    assert served["downloads"] == 2
    assert _paths(result) == ["src/new.js"]
    kept = [p.name for p in (tmp_path / "vomeadmin__vome-react").iterdir()]
    assert kept == ["b" * 40]


def test_a_failed_download_is_a_failure_not_an_empty_answer(served,
                                                            monkeypatch):
    def broken(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "stream", broken)
    result = code_mirror.search("vomeadmin", "vome-react", "views", {})
    assert not result.ok
    assert "ConnectError" in result.error


# ---------------------------------------------------------------------------
# Wiring into code_search.GitHub
# ---------------------------------------------------------------------------

def _github(monkeypatch, data):
    monkeypatch.setenv("VOMEOS_GITHUB_TOKEN", "t")
    client = code_search.GitHub()
    monkeypatch.setattr(
        client, "_call",
        lambda *a, **k: IntegrationResult("github", "search_code", True,
                                          data=data),
    )
    return client


def test_github_falls_back_only_when_its_index_is_incomplete(
        served, monkeypatch):
    client = _github(monkeypatch, {"items": [], "incomplete_results": True})
    result = client.search_code("TableColumnResizing", repo="vome-react",
                                owner="vomeadmin", ref="master")
    assert result.data.get("mirror")
    assert _paths(result) == ["src/views/MyVolunteerUserList.js"]


def test_github_answer_is_kept_when_the_index_is_complete(
        served, monkeypatch):
    client = _github(monkeypatch, {"items": [], "incomplete_results": False})
    result = client.search_code("nothing", repo="VomeApp", owner="vomeadmin")
    assert "mirror" not in result.data
    assert served["downloads"] == 0


def test_failed_fallback_returns_the_incomplete_result_with_the_reason(
        served, monkeypatch):
    monkeypatch.setattr(httpx, "stream",
                        lambda *a, **k: (_ for _ in ()).throw(
                            httpx.ReadTimeout("slow")))
    client = _github(monkeypatch, {"items": [], "incomplete_results": True})
    result = client.search_code("x", repo="vome-react", owner="vomeadmin")
    assert result.data["incomplete_results"] is True
    assert "ReadTimeout" in result.data["mirror_error"]


def test_the_mirror_cannot_write():
    # Same structural rule as code_search.py: reading only, by absence.
    source = inspect.getsource(code_mirror)
    for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in source
