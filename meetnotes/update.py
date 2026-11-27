"""In-app updates: a small wheel instead of a new installation.

The program is a ~250 KB Python package. Python, the speech model and the
libraries stay where they are. A release publishes `update.json`:

    {"version": "0.2.1", "notes": "...", "published": "2026-11-20",
     "wheel": {"url": "https://.../meetnotes-0.2.1-py3-none-any.whl", "sha256": "..."},
     "android": {"version_code": 3, "version": "1.2", "url": "https://.../Meetnotes.apk"},
     "min_python": "3.11"}

check() fetches it (at most once a day unless forced), apply() downloads the
wheel, verifies its SHA-256, installs it into this Python with pip, and asks
the background service to restart once the current meeting is over.
Where the update file lives is baked into the package at release time
(_release.json) and can be overridden with `update_url` in config.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, service
from .config import state_dir, write_json

log = logging.getLogger("meetnotes")
CHECK_INTERVAL = 24 * 3600


def version_tuple(v: str) -> tuple:
    parts = re.findall(r"\d+", str(v))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def release_info() -> dict:
    """What the release script baked in: {"update_url": ..., "android_version_code": ...}."""
    try:
        return json.loads((Path(__file__).parent / "_release.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_url(cfg_url: str = "") -> str:
    return (cfg_url or release_info().get("update_url") or "").strip()


def _state_path() -> Path:
    return state_dir() / "update.json"


def read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(**fields) -> dict:
    st = read_state()
    st.update(fields)
    write_json(_state_path(), st)
    return st


def fetch_manifest(url: str, timeout: float = 15.0) -> dict:
    if not url.startswith("https://"):
        raise ValueError("update address must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": f"meetnotes/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError("not an update file")
    return data


def check(cfg_url: str = "", force: bool = False, fetch=None) -> dict:
    """Returns the state: current, latest, available, notes, android, error, checked."""
    fetch = fetch or fetch_manifest
    url = update_url(cfg_url)
    st = read_state()
    if not url:
        return _write_state(current=__version__, configured=False, available=False, error="")
    last = st.get("checked_at_mono", 0)
    if not force and st.get("manifest") and time.monotonic() - last < CHECK_INTERVAL and st.get("url") == url:
        return _summary(st)
    try:
        manifest = fetch(url)
        st = _write_state(current=__version__, configured=True, url=url, manifest=manifest, error="",
                          checked=datetime.now().isoformat(timespec="seconds"), checked_at_mono=time.monotonic())
    except Exception as exc:
        st = _write_state(current=__version__, configured=True, url=url, error=str(exc)[:200],
                          checked=datetime.now().isoformat(timespec="seconds"), checked_at_mono=time.monotonic())
    return _summary(st)


def _summary(st: dict) -> dict:
    m = st.get("manifest") or {}
    latest = str(m.get("version", ""))
    android = m.get("android") or {}
    my_apk = int(release_info().get("android_version_code", 0) or 0)
    return {
        "current": __version__, "configured": bool(st.get("configured")), "latest": latest,
        "available": bool(latest) and version_tuple(latest) > version_tuple(__version__),
        "notes": str(m.get("notes", "")), "published": str(m.get("published", "")),
        "android": {"version": str(android.get("version", "")), "version_code": int(android.get("version_code", 0) or 0),
                    "url": str(android.get("url", "")), "newer_than_built": int(android.get("version_code", 0) or 0) > my_apk},
        "error": st.get("error", ""), "checked": st.get("checked", ""),
        "progress": st.get("progress", ""), "progress_error": st.get("progress_error", ""),
        "installed": st.get("installed", ""),
    }


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, timeout: float = 120.0) -> None:
    if not url.startswith("https://"):
        raise ValueError("download address must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": f"meetnotes/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:
        for chunk in iter(lambda: r.read(1 << 16), b""):
            out.write(chunk)


def pip_install(wheel: Path, python: str | None = None) -> str:
    cmd = [python or sys.executable, "-m", "pip", "install", "--upgrade", "--upgrade-strategy", "only-if-needed",
           "--no-input", "--disable-pip-version-check", str(wheel)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError("pip: " + " | ".join(tail))
    return r.stdout


def apply(cfg_url: str = "", fetch=None, downloader=None, installer=None) -> dict:
    """Download, verify, install, then ask the service to restart. Blocking;
    the server runs it in a thread and the app polls progress."""
    fetch, downloader, installer = fetch or fetch_manifest, downloader or download, installer or pip_install
    _write_state(progress="checking", progress_error="")
    try:
        manifest = fetch(update_url(cfg_url))
        wheel = manifest.get("wheel") or {}
        url, digest = str(wheel.get("url", "")), str(wheel.get("sha256", "")).lower()
        if not url.startswith("https://") or len(digest) != 64:
            raise ValueError("update file has no wheel")
        if version_tuple(manifest["version"]) <= version_tuple(__version__):
            raise ValueError(f"already on {__version__}")
        _write_state(progress="downloading")
        with tempfile.TemporaryDirectory(prefix="meetnotes-update-") as tmp:
            dest = Path(tmp) / url.rsplit("/", 1)[-1].split("?")[0]
            downloader(url, dest)
            if sha256_of(dest) != digest:
                raise ValueError("downloaded file does not match its checksum; not installed")
            _write_state(progress="installing")
            installer(dest)
        _write_state(progress="restarting", installed=str(manifest["version"]), manifest=manifest)
        service.request_restart()
        return _summary(read_state())
    except Exception as exc:
        log.warning("update failed: %s", exc)
        st = _write_state(progress="failed", progress_error=str(exc)[:300])
        return _summary(st)


_lock = threading.Lock()


def apply_in_background(cfg_url: str = "") -> bool:
    """Start apply() unless one is running. Returns False if busy."""
    if not _lock.acquire(blocking=False):
        return False

    def run():
        try:
            apply(cfg_url)
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True, name="update").start()
    return True
