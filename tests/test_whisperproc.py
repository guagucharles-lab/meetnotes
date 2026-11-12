"""The speech worker: real subprocesses, with a fake Whisper that crashes,
hangs or fails the way broken setups do."""
import json

import numpy as np
import pytest

from meetnotes import whisperproc as wp

SPEECH = (np.sin(np.arange(16000 * 6) / 7) * 4000).astype(np.int16)  # 6 s of "sound"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETNOTES_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    (tmp_path / "state").mkdir()
    wp.STATE.clear()
    yield tmp_path


def fake(monkeypatch, tmp_path, **spec):
    spec["log"] = str(tmp_path / "loads.txt")
    monkeypatch.setenv("MEETNOTES_FAKE_WHISPER", json.dumps(spec))
    return tmp_path / "loads.txt"


def test_gpu_crash_falls_back_to_cpu_and_is_remembered(monkeypatch, isolated):
    loads = fake(monkeypatch, isolated, crash_on_device="cuda")
    t = wp.IsolatedTranscriber("large-v3-turbo", "auto", "auto", "fi")
    try:
        segs = t.transcribe(SPEECH, 10.0, "Me")
    finally:
        t.close()
    assert len(segs) == 1 and segs[0].text == "[large-v3-turbo/cpu] puhetta"
    assert segs[0].start == 10.0 and segs[0].speaker == "Me"
    assert loads.read_text().split() == ["large-v3-turbo|cuda", "large-v3-turbo|cpu"]
    # the crash left its message in the worker log, and the service lived on
    assert "cudnn" in wp.worker_log_path().read_text()
    # next time it goes straight to the working setup
    t2 = wp.IsolatedTranscriber("large-v3-turbo", "auto", "auto", "fi")
    try:
        assert t2.setup == ("large-v3-turbo", "cpu", "int8")
        t2.transcribe(SPEECH, 0.0)
    finally:
        t2.close()
    assert loads.read_text().split()[-1] == "large-v3-turbo|cpu"


def test_stuck_model_load_falls_back_to_smaller_model(monkeypatch, isolated):
    loads = fake(monkeypatch, isolated, hang_model="large-v3-turbo")
    monkeypatch.setattr(wp, "LOAD_STALL_S", 2)
    t = wp.IsolatedTranscriber("large-v3-turbo", "cpu", "int8")
    try:
        segs = t.transcribe(SPEECH, 0.0)
    finally:
        t.close()
    assert segs[0].text == "[small/cpu] puhetta"
    assert t.model_size == "small"
    assert "stuck" in json.loads(wp._memory_path().read_text())["large-v3-turbo|cpu|int8"]["reason"]
    assert loads.read_text().split() == ["large-v3-turbo|cpu", "small|cpu"]


def test_nothing_works_raises_clear_error(monkeypatch, isolated):
    fake(monkeypatch, isolated, crash_load_model="small", error_model="base")
    t = wp.IsolatedTranscriber("small", "cpu", "int8")
    with pytest.raises(wp.WorkerFailed, match="does not work on this computer"):
        t.transcribe(SPEECH, 0.0)
    t.close()
    assert wp.state()["phase"] == "failed"
    assert not wp._memory_path().exists()  # nothing that works was found, so nothing remembered


def test_idle_worker_exits_and_restarts_without_fallback(monkeypatch, isolated):
    import time
    loads = fake(monkeypatch, isolated)
    monkeypatch.setenv("MEETNOTES_WORKER_IDLE", "0.5")
    t = wp.IsolatedTranscriber("large-v3-turbo", "cpu", "int8")
    try:
        t.transcribe(SPEECH, 0.0)
        pid = t._proc.pid
        time.sleep(1.5)  # the worker quits to free memory
        assert t._proc.poll() == 0
        segs = t.transcribe(SPEECH, 6.0)
        assert t._proc.pid != pid
    finally:
        t.close()
    assert segs[0].text == "[large-v3-turbo/cpu] puhetta"
    assert loads.read_text().split() == ["large-v3-turbo|cpu"] * 2
    assert not wp._memory_path().exists()


def test_silence_does_not_start_a_worker(monkeypatch, isolated):
    fake(monkeypatch, isolated)
    t = wp.IsolatedTranscriber("small", "cpu", "int8")
    assert t.transcribe(np.zeros(16000 * 5, np.int16), 0.0) == []
    assert t._proc is None


def test_self_test_records_result_and_fallback(monkeypatch, isolated):
    from meetnotes import session
    from meetnotes.config import Config
    fake(monkeypatch, isolated, crash_on_device="cuda")
    monkeypatch.setattr(session, "_TRANSCRIBERS", {})
    cfg = Config(whisper_model="large-v3-turbo")
    assert wp.needs_test(cfg)
    res = wp.run_self_test(cfg)
    assert res["ok"] and res["device"] == "cpu"
    assert not wp.needs_test(cfg)
    inf = wp.info(cfg)
    assert inf["setup"]["fallback"] and inf["setup"]["device"] == "cpu" and "crashed" in inf["setup"]["reason"]
    text = wp.diagnostics_text(cfg)
    assert "speech-worker.log" in text and "cudnn" in text
    wp.reset(cfg)
    assert not wp.info(cfg)["setup"]["fallback"]


def test_hang_while_transcribing_is_killed_and_retried(monkeypatch, isolated):
    loads = fake(monkeypatch, isolated, hang_transcribe_model="medium")
    monkeypatch.setattr(wp, "TRANSCRIBE_TIMEOUT_MIN_S", 2)
    t = wp.IsolatedTranscriber("medium", "cpu", "int8")
    try:
        segs = t.transcribe(SPEECH[:16000], 0.0)  # 1 s: 12 s timeout
    finally:
        t.close()
    assert segs[0].text == "[small/cpu] puhetta"
    assert loads.read_text().split() == ["medium|cpu", "small|cpu"]


def test_stale_summarizing_shows_as_interrupted(tmp_path):
    from meetnotes.server import App
    app = App.__new__(App)
    app.summarizing = set()
    app.phone = None
    assert app._shown_status("x", "summarizing") == "interrupted"  # the process that wrote it is gone
    app.summarizing.add("x")
    assert app._shown_status("x", "summarizing") == "summarizing"
