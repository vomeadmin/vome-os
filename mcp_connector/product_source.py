"""
mcp_connector/product_source.py

The frontend source, on a box that has no checkout of it.

THE GAP THIS CLOSES
-------------------
`product_strings.py` and `product_navigation.py` are the two best answers to
"does this exist and where is it", and both read a sibling checkout of
vome-react. On a laptop that checkout exists. On Railway it does not, so in
production both modules have only ever reported "NOT searched". The
connector is the first production caller that needs them, so it has to bring
the source with it.

HOW
---
One GitHub tarball of each repository at its LIVE branch (repos.py), with
only what the two modules read kept on disk: translations, routers and the
view tree. Refreshed every MCP_PRODUCT_SOURCE_TTL seconds, so a string that
shipped this morning is answerable this afternoon.

`code_search.py` argues against clones because a clone drifts silently.
This one is pinned to a named branch, refreshed on a timer, and reported in
`status()` with the time it was fetched, so drift is bounded and visible.

When a local checkout exists (VOME_REACT_PATH set, or the sibling directory
present) it is used as-is and nothing is downloaded.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import httpx

from mcp_connector import config, repos

_lock = threading.Lock()
_state: dict[str, object] = {
    "mode": "unknown",
    "fetched_at": 0.0,
    "web_ref": "",
    "mobile_ref": "",
    "error": "",
}

CACHE_ROOT = Path(tempfile.gettempdir()) / "vome_product_source"

# What product_strings and product_navigation actually read.
_WEB_KEEP = ("src/translations/", "src/routers/", "src/views/")
_MOBILE_KEEP = ("src/translations/",)


def _local_checkout() -> bool:
    import product_navigation

    return product_navigation.react_root() is not None


def _github_token(owner: str) -> str:
    from vomeos.integrations.code_search import GitHub

    return GitHub().token_for(owner)


def _download(repo: repos.Repo, keep: tuple[str, ...], dest: Path) -> None:
    """Fetch one repo's tarball at its live ref, keeping only `keep` paths."""
    token = _github_token(repo.owner)
    if not token:
        raise RuntimeError(f"no GitHub token for {repo.owner}")

    url = (
        f"https://api.github.com/repos/{repo.owner}/{repo.slug}"
        f"/tarball/{repo.live_ref}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    staging = dest.with_name(dest.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    with tempfile.TemporaryFile() as archive:
        with httpx.stream(
            "GET", url, headers=headers, follow_redirects=True, timeout=120,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                archive.write(chunk)
        archive.seek(0)

        with tarfile.open(fileobj=archive, mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                # Strip GitHub's "<owner>-<repo>-<sha>/" prefix.
                _, _, relative = member.name.partition("/")
                if not relative.endswith(".js"):
                    continue
                if not relative.startswith(keep):
                    continue
                target = (staging / relative).resolve()
                if staging.resolve() not in target.parents:
                    continue  # path traversal in an archive: skip it
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    continue
                with source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)

    # Swap in whole, so a reader never sees half a tree.
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    staging.rename(dest)
    shutil.rmtree(old, ignore_errors=True)


def _clear_caches() -> None:
    import product_navigation
    import product_strings

    product_strings._strings.cache_clear()
    product_navigation._routes.cache_clear()
    product_navigation._component_index.cache_clear()


def ensure_fresh(force: bool = False) -> dict:
    """Make the frontend source readable. Cheap when already fresh.

    Never raises: a failed refresh leaves the previous copy in place and is
    reported in status(), because stale-by-hours is better than nothing and
    the product modules already say plainly when they did not look.
    """
    with _lock:
        if _state["mode"] == "local":
            return status()
        if _state["mode"] == "unknown" and _local_checkout():
            _state["mode"] = "local"
            return status()

        age = time.time() - float(_state["fetched_at"] or 0)
        fresh = _state["fetched_at"] and age < config.product_source_ttl()
        if fresh and not force:
            return status()

        web = repos.resolve("vome-react")
        mobile = repos.resolve("VomeApp")
        web_dir = CACHE_ROOT / "vome-react"
        mobile_dir = CACHE_ROOT / "VomeApp"
        errors = []

        try:
            if web is None:
                raise RuntimeError("vome-react is not in the repo registry")
            _download(web, _WEB_KEEP, web_dir)
            os.environ["VOME_REACT_PATH"] = str(
                web_dir / "src" / "translations"
            )
            _state["web_ref"] = web.live_ref
        except Exception as exc:
            errors.append(f"web: {exc}")

        try:
            if mobile is None:
                raise RuntimeError("VomeApp is not in the repo registry")
            _download(mobile, _MOBILE_KEEP, mobile_dir)
            os.environ["VOME_APP_PATH"] = str(
                mobile_dir / "src" / "translations"
            )
            _state["mobile_ref"] = mobile.live_ref
        except Exception as exc:
            errors.append(f"mobile: {exc}")

        _state["mode"] = "downloaded"
        _state["error"] = "; ".join(errors)
        # Even a total failure counts as an attempt, so a broken token is
        # retried on the TTL rather than on every single tool call.
        _state["fetched_at"] = time.time()
        if errors:
            print(f"[MCP SOURCE] refresh incomplete: {_state['error']}")
        _clear_caches()
        return status()


def refresh_in_background() -> dict:
    """status() now, with a refresh started if the copy is stale.

    For callers that cannot wait on a download. Vic has 30 seconds for a
    whole chat turn, and ensure_fresh holds the lock for as long as the
    tarball takes, so Vic reads whatever copy is on disk and the refresh
    lands for the next question. Starts nothing while another refresh holds
    the lock.
    """
    if _state["mode"] == "local":
        return status()
    if _state["mode"] == "unknown" and _local_checkout():
        return ensure_fresh()
    age = time.time() - float(_state["fetched_at"] or 0)
    if _state["fetched_at"] and age < config.product_source_ttl():
        return status()
    if _lock.acquire(blocking=False):
        _lock.release()
        threading.Thread(
            target=ensure_fresh, name="vic-source", daemon=True,
        ).start()
    return status()


def status() -> dict:
    fetched = float(_state["fetched_at"] or 0)
    return {
        "mode": _state["mode"],
        "web_branch": _state["web_ref"] or None,
        "mobile_branch": _state["mobile_ref"] or None,
        "fetched_minutes_ago": (
            round((time.time() - fetched) / 60) if fetched else None
        ),
        "error": _state["error"] or None,
    }
