from meetnotes import desktop, winsetup
from meetnotes.config import Config, load
from meetnotes.server import App


def test_first_run_config_is_finnish_and_never_overwrites(tmp_path):
    path = tmp_path / "config.toml"
    assert winsetup.first_run_config(path) is True
    cfg = load(path)
    assert (cfg.language, cfg.notes_language, cfg.whisper_model) == ("fi", "Finnish", "large-v3-turbo")
    path.write_text('language = "en"\n')
    assert winsetup.first_run_config(path) is False
    assert load(path).language == "en"


def test_desktop_finds_running_service(tmp_path, monkeypatch):
    cfg = Config(notes_dir=str(tmp_path / "n"))
    app = App(cfg, config_path=tmp_path / "c.toml")
    app.start(port=0)
    try:
        cfg.app_port = app.local.server_address[1]
        started = []
        monkeypatch.setattr(desktop.service, "start_background", lambda *a: started.append(a))
        assert desktop.ensure_service(cfg) is True
        assert started == []  # already running: nothing launched
        st = desktop._status(cfg.app_port, app.token)
        assert st["state"] == "watching"
        desktop._api(cfg.app_port, app.token, "/api/pause", {"on": True})
        assert desktop.service.is_paused()
    finally:
        app.stop()


def test_desktop_starts_service_when_missing(tmp_path, monkeypatch):
    cfg = Config(app_port=1)  # nothing listens on port 1
    started = []
    monkeypatch.setattr(desktop.service, "start_background", lambda *a: started.append(a))
    assert desktop.ensure_service(cfg, timeout=0.5) is False
    assert len(started) == 1
