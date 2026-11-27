"""Desktop app: a native window (Edge WebView2 on Windows) showing the app the
background service serves on 127.0.0.1, plus an optional notification-area
icon. Closing the window only hides it; recording goes on in the background
service either way.

Start it with the "Meetnotes" shortcut, `meetnotes-app`, or `meetnotes app`.
"""
from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config as config_mod, service

log = logging.getLogger("meetnotes")
WEB = Path(__file__).parent / "web"


def _ping(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "meetnotes"
    except Exception:
        return False


def ensure_service(cfg, config_path: Path | None = None, timeout: float = 20.0) -> bool:
    """Start the background service if needed and wait until the app answers."""
    if _ping(cfg.app_port):
        return True
    service.start_background(config_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ping(cfg.app_port):
            return True
        time.sleep(0.3)
    return False


def _api(port: int, token: str, path: str, data: dict) -> dict:
    from .server import app_token
    token = app_token()  # read each time: "forget devices" replaces it
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Meetnotes-Token": token}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _status(port: int, token: str) -> dict:
    from .server import app_token
    token = app_token()  # read each time: "forget devices" replaces it
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status",
                                 headers={"X-Meetnotes-Token": token})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def _tray(window, cfg, token):  # pragma: no cover - needs a desktop
    import pystray
    from PIL import Image

    port = cfg.app_port
    icon_img = Image.open(WEB / "icon-192.png")

    def recording() -> bool:
        try:
            return _status(port, token).get("state") in ("recording", "paused")
        except Exception:
            return False

    def toggle_record(icon, item):
        _api(port, token, "/api/record", {"on": not recording()})

    def toggle_pause(icon, item):
        _api(port, token, "/api/pause", {"on": not service.is_paused()})

    def show(icon, item):
        window.show()
        window.restore()

    def quit_app(icon, item):
        icon.stop()
        window.events.closing -= _hide_instead
        window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("Avaa Meetnotes", show, default=True),
        pystray.MenuItem(lambda item: "Lopeta nauhoitus" if recording() else "Nauhoita nyt", toggle_record),
        pystray.MenuItem(lambda item: "Jatka" if service.is_paused() else "Tauko", toggle_pause,
                         enabled=lambda item: recording()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sulje ikkuna (nauhoitus jatkuu taustalla)", quit_app),
    )
    icon = pystray.Icon("meetnotes", icon_img, "Meetnotes", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def _hide_instead(window):  # pragma: no cover - needs a desktop
    window.hide()
    return False  # cancel the close


def main(config_path: Path | None = None) -> int:
    cfg = config_mod.load(config_path)
    if not ensure_service(cfg, config_path):
        msg = (f"The background service did not start. Log: {service.log_path()}")
        log.error(msg)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Meetnotes", 0x10)
        except Exception:
            print(msg, file=sys.stderr)
        return 1
    from .server import app_token
    token = app_token()
    url = f"http://127.0.0.1:{cfg.app_port}/?t={token}"

    try:
        import webview
    except ImportError:
        webbrowser.open(url)  # no native window available: use the browser
        return 0

    storage = service.state_dir() / "webview"
    window = webview.create_window("Meetnotes", url, width=1120, height=780, min_size=(380, 560),
                                   background_color="#0f1115")
    use_tray = cfg.tray_icon and platform.system() == "Windows"
    if use_tray:
        window.events.closing += _hide_instead

        def started():
            _tray(window, cfg, token)
        webview.start(started, private_mode=False, storage_path=str(storage))
    else:
        webview.start(private_mode=False, storage_path=str(storage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
