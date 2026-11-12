#!/usr/bin/env python3
"""Build a release: wheel, installers, Android APK and update.json.

    python release.py --owner <github-user-or-org> [--repo meetnotes] [--apk-version-code N]

Writes dist/:
  meetnotes-<version>-py3-none-any.whl   the program (what in-app updates download)
  Meetnotes-Windows.zip                  source + windows/ installer
  Meetnotes-macOS.zip                    source + mac/ installer
  Meetnotes.apk                          Android app (if the Android tools are installed)
  update.json                            what installed copies poll

Upload all of them as assets of a GitHub release tagged v<version>. Installed
copies read https://github.com/<owner>/<repo>/releases/latest/download/update.json,
so every later release at that address is found automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
EXCLUDE_DIRS = {"dist", "build", "__pycache__", ".pytest_cache", ".git", "android/build"}


def version() -> str:
    return re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    for f in sorted(ROOT.rglob("*")):
        rel = f.relative_to(ROOT)
        if not f.is_file() or any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if rel.parts[:2] == ("android", "build") or rel.name in ("meetnotes.keystore",):
            continue
        yield f, rel


def zip_source(name: str, keep_dirs: set[str]) -> Path:
    out = DIST / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f, rel in source_files():
            top = rel.parts[0]
            if top in ("windows", "mac", "android") and top not in keep_dirs:
                continue
            z.write(f, Path("meetnotes") / rel)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True, help="GitHub user or organisation")
    ap.add_argument("--repo", default="meetnotes")
    ap.add_argument("--apk-version-code", type=int, default=None, help="increase when the Android app itself changed")
    ap.add_argument("--notes", default="", help="one line of release notes shown in the app")
    args = ap.parse_args()
    v = version()
    base = f"https://github.com/{args.owner}/{args.repo}/releases/latest/download"
    DIST.mkdir(exist_ok=True)
    for old in DIST.iterdir():
        old.unlink() if old.is_file() else shutil.rmtree(old)

    # 1) bake the update address and the APK version into the package
    release = json.loads((ROOT / "meetnotes" / "_release.json").read_text()) if (ROOT / "meetnotes" / "_release.json").exists() else {}
    apk_code = args.apk_version_code or int(release.get("android_version_code", 1))
    release.update(update_url=f"{base}/update.json", android_version_code=apk_code, version=v)
    (ROOT / "meetnotes" / "_release.json").write_text(json.dumps(release, indent=2) + "\n")

    # 2) wheel
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(DIST), str(ROOT)], check=True)
    wheel = next(DIST.glob("meetnotes-*.whl"))

    # 3) installers (source + platform folder)
    win = zip_source("Meetnotes-Windows.zip", {"windows"})
    mac = zip_source("Meetnotes-macOS.zip", {"mac"})

    # 4) Android (optional: needs aapt, dalvik-exchange, zipalign, apksigner, javac)
    apk = None
    manifest = ROOT / "android" / "AndroidManifest.xml"
    text = re.sub(r'android:versionCode="\d+"', f'android:versionCode="{apk_code}"', manifest.read_text())
    text = re.sub(r'android:versionName="[^"]+"', f'android:versionName="{v}"', text)
    manifest.write_text(text)
    if not (ROOT / "android" / "meetnotes.keystore").exists() and os.environ.get("CI"):
        # A new key would make Android refuse to update the installed app.
        print("APK not built: no signing key (set the ANDROID_KEYSTORE_B64 secret)")
    elif shutil.which("aapt") and shutil.which("apksigner"):
        r = subprocess.run(["sh", str(ROOT / "android" / "build.sh")], capture_output=True, text=True)
        if r.returncode == 0:
            apk = DIST / "Meetnotes.apk"
            shutil.copy(ROOT / "android" / "build" / "Meetnotes.apk", apk)
        else:
            print("APK not built:", r.stderr.strip().splitlines()[-1:] or r.stdout[-300:])
    else:
        print("APK not built: Android tools not installed (apt-get install aapt apksigner dalvik-exchange zipalign android-sdk-platform-23)")

    # 5) update.json
    info = {
        "version": v, "published": date.today().isoformat(), "notes": args.notes, "min_python": "3.11",
        "wheel": {"url": f"{base}/{wheel.name}", "sha256": sha256(wheel)},
        "installers": {"windows": f"{base}/{win.name}", "macos": f"{base}/{mac.name}"},
        "android": {"version_code": apk_code, "version": v, "url": f"{base}/Meetnotes.apk" if apk else ""},
    }
    (DIST / "update.json").write_text(json.dumps(info, indent=2) + "\n")

    print(f"\nRelease {v} in {DIST}:")
    for f in sorted(DIST.iterdir()):
        print(f"  {f.name:40} {f.stat().st_size // 1024:6} KB")
    print(f"\nUpload them to https://github.com/{args.owner}/{args.repo}/releases/new?tag=v{v}")
    print("or push the tag and let .github/workflows/release.yml do it:  git tag v" + v + " && git push --tags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
