"""meetnotes - a private, local meeting note-taker.

It listens to the meeting audio on *your* machine (what you hear plus your
microphone), transcribes it locally with Whisper, and asks Claude to write the
notes. Nothing joins the call, so other participants never see it.
"""

__version__ = "0.2.4"

import os as _os
import sys as _sys

# Windows starts the app and the background service with pythonw.exe, which
# has no console: sys.stdout/sys.stderr are None and any library that prints
# a progress bar (e.g. while downloading the speech model) would crash.
if _sys.stdout is None or _sys.stderr is None:
    _null = open(_os.devnull, "w", encoding="utf-8")
    _sys.stdout = _sys.stdout or _null
    _sys.stderr = _sys.stderr or _null
    _os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
