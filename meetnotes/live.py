"""The meeting being recorded right now, shared between the watcher loop and
the app server (same process), so the app can talk to its assistant."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_current = None


def set_current(session) -> None:
    global _current
    with _lock:
        _current = session


def clear(session=None) -> None:
    global _current
    with _lock:
        if session is None or _current is session:
            _current = None


def current():
    with _lock:
        return _current
