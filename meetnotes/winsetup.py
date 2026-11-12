"""Steps the Windows installer runs with the app's own Python:

    python -m meetnotes.winsetup config   # Finnish defaults, only on first install
    python -m meetnotes.winsetup model    # download the speech model now, not in the first meeting
"""
from __future__ import annotations

import sys

from . import config as config_mod

FINNISH_DEFAULTS = {
    "language": "fi",
    "notes_language": "Finnish",
    "whisper_model": "large-v3-turbo",
}


def first_run_config(path=None) -> bool:
    """Write a config with Finnish defaults unless one exists. True if written."""
    path = path or config_mod.config_path()
    if path.exists():
        return False
    cfg = config_mod.Config()
    for key, value in FINNISH_DEFAULTS.items():
        setattr(cfg, key, value)
    config_mod.save(cfg, path)
    return True


def download_model(cfg=None) -> str:
    from faster_whisper.utils import download_model as fetch
    cfg = cfg or config_mod.load()
    return fetch(cfg.whisper_model)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    step = argv[0] if argv else ""
    if step == "config":
        written = first_run_config()
        print("Asetukset luotu (suomi)." if written else "Aiemmat asetukset säilytetty.")
        return 0
    if step == "model":
        cfg = config_mod.load()
        print(f"Ladataan puheentunnistusmalli '{cfg.whisper_model}' (kerran, voi kestää muutaman minuutin)...")
        download_model(cfg)
        print("Malli valmis.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
