import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meetnotes import __version__, service, update


def manifest(version="9.9.9", sha="", url="https://example.com/meetnotes-9.9.9-py3-none-any.whl"):
    return {"version": version, "notes": "Uusi avustaja", "published": "2026-11-20",
            "wheel": {"url": url, "sha256": sha or "0" * 64},
            "android": {"version_code": 99, "version": "0.3", "url": "https://example.com/Meetnotes.apk"}}


def test_version_tuple():
    assert update.version_tuple("0.2.10") > update.version_tuple("0.2.9") > update.version_tuple("0.2")
    assert update.version_tuple("v1.0.0") == (1, 0, 0)


def test_check_uses_cache_and_reports(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return manifest()
    st = update.check("https://example.com/update.json", force=True, fetch=fetch)
    assert st["available"] and st["latest"] == "9.9.9" and st["current"] == __version__ and st["notes"] == "Uusi avustaja"
    assert st["android"]["newer_than_built"] is True and st["android"]["url"].endswith(".apk")
    update.check("https://example.com/update.json", fetch=fetch)
    assert len(calls) == 1                                   # cached for a day
    update.check("https://example.com/update.json", force=True, fetch=fetch)
    assert len(calls) == 2
    st = update.check("https://example.com/update.json", force=True, fetch=lambda u: manifest(version=__version__))
    assert st["available"] is False and st["latest"] == __version__
    st = update.check("https://example.com/update.json", force=True, fetch=lambda u: (_ for _ in ()).throw(OSError("timeout")))
    assert "timeout" in st["error"] and st["latest"] == __version__   # keeps the last manifest
    # a build without a baked-in address and none in the settings: updates are off
    monkeypatch.setattr(update, "release_info", lambda: {})
    assert update.check("", force=True, fetch=fetch)["configured"] is False
    with pytest.raises(ValueError):
        update.fetch_manifest("http://insecure.example/update.json")


def test_apply_verifies_checksum_installs_and_requests_restart(tmp_path):
    payload = b"fake wheel bytes"
    good = hashlib.sha256(payload).hexdigest()
    installed = []

    def downloader(url, dest):
        dest.write_bytes(payload)
    st = update.apply("https://example.com/update.json", fetch=lambda u: manifest(sha=good),
                      downloader=downloader, installer=lambda wheel: installed.append(wheel.name))
    assert st["progress"] == "restarting" and st["installed"] == "9.9.9" and installed == ["meetnotes-9.9.9-py3-none-any.whl"]
    assert service.restart_requested()
    service.clear_restart()

    # wrong checksum: nothing installed, no restart
    installed.clear()
    st = update.apply("https://example.com/update.json", fetch=lambda u: manifest(sha="f" * 64),
                      downloader=downloader, installer=lambda wheel: installed.append(wheel.name))
    assert st["progress"] == "failed" and "checksum" in st["progress_error"] and installed == [] and not service.restart_requested()

    # older or same version: refused
    st = update.apply("https://example.com/update.json", fetch=lambda u: manifest(version="0.0.1", sha=good),
                      downloader=downloader, installer=lambda w: None)
    assert st["progress"] == "failed" and "already" in st["progress_error"]

    # pip failure is reported
    def bad_pip(wheel):
        raise RuntimeError("pip: No matching distribution")
    st = update.apply("https://example.com/update.json", fetch=lambda u: manifest(sha=good), downloader=downloader, installer=bad_pip)
    assert st["progress"] == "failed" and "pip" in st["progress_error"]


def test_pip_install_real_wheel(tmp_path):
    """Install a real (tiny) wheel into a throwaway venv with the same command the updater uses."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # bootstrap pip into the venv from this interpreter's pip
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(venv / "lib" / "site"), "pip"], capture_output=True)
    if r.returncode != 0:
        pytest.skip("pip not available for a venv here")
    pkg = tmp_path / "pkg"
    (pkg / "tinymod").mkdir(parents=True)
    (pkg / "tinymod" / "__init__.py").write_text("VERSION = '2'\n")
    (pkg / "pyproject.toml").write_text('[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n[project]\nname="tinymod"\nversion="2"\n')
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "-w", str(tmp_path), str(pkg)], check=True)
    wheel = next(tmp_path.glob("tinymod-2-*.whl"))
    env = {**os.environ, "PYTHONPATH": str(venv / "lib" / "site")}
    # Use the updater's command through the venv python (pip found via PYTHONPATH)
    cmd = [str(py), "-m", "pip", "install", "--upgrade", "--upgrade-strategy", "only-if-needed", "--no-input",
           "--disable-pip-version-check", str(wheel)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stderr[-500:]
    v = subprocess.run([str(py), "-c", "import tinymod; print(tinymod.VERSION)"], capture_output=True, text=True).stdout.strip()
    assert v == "2"


def test_watcher_restart_cycle(monkeypatch, tmp_path):
    """restart_background releases the pid file and starts a new watcher."""
    started = []
    monkeypatch.setattr(service, "start_background", lambda cfg=None: (started.append(cfg) or (4321, True)))
    service.write_pid()
    service.request_restart()
    pid = service.restart_background(Path("c.toml"))
    assert pid == 4321 and started == [Path("c.toml")]
    assert not service.restart_requested() and service.running_pid() is None


def test_server_update_endpoints(tmp_path, monkeypatch):
    from tests.test_server import call
    from meetnotes.server import App
    from meetnotes.config import Config
    monkeypatch.setattr(update, "fetch_manifest", lambda url, timeout=15.0: manifest())
    cfg = Config(notes_dir=str(tmp_path / "n"), update_url="https://example.com/update.json")
    app = App(cfg, config_path=tmp_path / "c.toml")
    app.start(port=0)
    try:
        st, u = call(app, "POST", "/api/update/check", {})
        assert st == 200 and u["available"] and u["latest"] == "9.9.9"
        assert call(app, "GET", "/api/status")[1]["update_available"] == "9.9.9"
        applied = {}
        monkeypatch.setattr(update, "apply", lambda cfg_url: applied.setdefault("url", cfg_url))
        assert call(app, "POST", "/api/update/apply", {})[0] == 202
        for _ in range(50):
            if applied:
                break
            time.sleep(0.05)
        assert applied["url"] == "https://example.com/update.json"
        monkeypatch.setattr(update, "fetch_manifest", lambda url, timeout=15.0: manifest(version=__version__))
        call(app, "POST", "/api/update/check", {})
        assert call(app, "POST", "/api/update/apply", {})[0] == 400
    finally:
        app.stop()


def test_release_script_builds_manifest(tmp_path):
    """release.py end to end (wheel + zips + update.json), APK skipped if tools are missing."""
    import shutil
    root = Path(__file__).resolve().parent.parent
    work = tmp_path / "src"
    shutil.copytree(root, work, ignore=shutil.ignore_patterns("dist", "build", "__pycache__", ".pytest_cache", ".git", "*.egg-info"))
    env = {**os.environ, "PATH": "/nonexistent"}  # hide aapt/apksigner so the APK step is skipped
    r = subprocess.run([sys.executable, str(work / "release.py"), "--owner", "testuser", "--notes", "Testi"],
                       capture_output=True, text=True, cwd=work, env={**os.environ, "PATH": os.environ["PATH"].replace("/usr/bin", "/nonexistent")}, timeout=600)
    assert r.returncode == 0, r.stderr[-800:]
    dist = work / "dist"
    names = {p.name for p in dist.iterdir()}
    assert "Meetnotes-Windows.zip" in names and "Meetnotes-macOS.zip" in names and "update.json" in names
    wheel = next(dist.glob("meetnotes-*.whl"))
    info = json.loads((dist / "update.json").read_text())
    assert info["wheel"]["url"] == f"https://github.com/testuser/meetnotes/releases/latest/download/{wheel.name}"
    assert info["wheel"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest() and info["notes"] == "Testi"
    baked = json.loads((work / "meetnotes" / "_release.json").read_text())
    assert baked["update_url"].endswith("/releases/latest/download/update.json")
    import zipfile
    with zipfile.ZipFile(wheel) as z:
        assert "meetnotes/_release.json" in z.namelist() and "meetnotes/web/app.js" in z.namelist()
    with zipfile.ZipFile(dist / "Meetnotes-macOS.zip") as z:
        n = z.namelist()
        assert "meetnotes/mac/Asenna Meetnotes.command" in n and not any("/windows/" in x for x in n)
        assert not any("keystore" in x for x in n)
