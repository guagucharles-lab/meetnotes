import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own state dir so pause/stop flags never leak."""
    monkeypatch.setenv("MEETNOTES_STATE_DIR", str(tmp_path / "_state"))
