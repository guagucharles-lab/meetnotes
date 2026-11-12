#!/bin/bash
# Meetnotes: asennus macOS:lle. Tuplaklikkaa Finderissa.
# Jos macOS sanoo "ei voida avata, koska kehittäjää ei voida tunnistaa":
# napsauta tiedostoa Ctrl-näppäin pohjassa, valitse Avaa ja sitten Avaa uudelleen.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$(cd "$HERE/.." && pwd)"
APPDIR="$HOME/Library/Application Support/meetnotes"
VENV="$APPDIR/venv"
PY="$VENV/bin/python3"
APP="$HOME/Applications/Meetnotes.app"

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mAsennus keskeytyi: %s\033[0m\n' "$1"; read -r -p "Paina Enter sulkeaksesi." _; exit 1; }

echo "Meetnotes - asennus macOS:lle"
echo "Lähde: $SOURCE"
[ -f "$SOURCE/pyproject.toml" ] || fail "pyproject.toml puuttuu. Pura koko meetnotes-kansio ja aja tämä sen mac-kansiosta."
# Files from a downloaded zip carry the quarantine flag; clear it for this folder.
xattr -dr com.apple.quarantine "$SOURCE" 2>/dev/null || true

step "Python"
PYTHON=""
for c in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PYTHON="$c"; break; fi
done
if [ -z "$PYTHON" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Asennetaan Python Homebrew'lla..."
    brew install python@3.12 || true
    PYTHON="$(brew --prefix)/bin/python3"
  fi
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || { open "https://www.python.org/downloads/macos/"; fail "Python 3.11 tai uudempi puuttuu. Asenna se avautuvalta sivulta ja aja tämä uudelleen."; }
echo "Käytetään: $PYTHON ($("$PYTHON" --version))"

if [ -x "$PY" ]; then
  step "Pysäytetään aiempi versio"
  "$PY" -m meetnotes.cli stop >/dev/null 2>&1 || true
fi

step "Asennetaan Meetnotes (ensimmäisellä kerralla muutama minuutti)"
mkdir -p "$APPDIR"
[ -x "$PY" ] || "$PYTHON" -m venv "$VENV"
"$PY" -m pip install --upgrade --quiet pip
"$PY" -m pip install --upgrade --quiet "$SOURCE[app]" || fail "Pakettien asennus epäonnistui. Tarkista nettiyhteys ja aja uudelleen."

step "Kuulokkeiden äänen kaappaus (BlackHole)"
if [ -d "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver" ]; then
  echo "BlackHole on jo asennettu."
elif command -v brew >/dev/null 2>&1; then
  brew install blackhole-2ch || echo "BlackHolen asennus ei onnistunut nyt; asenna se myöhemmin: brew install blackhole-2ch"
else
  echo "Asenna BlackHole 2ch osoitteesta https://existential.audio/blackhole/ (avataan selaimeen)."
  open "https://existential.audio/blackhole/"
fi
echo "Muista: Ääni-MIDI-asetukset > + > Luo usean ulostulon laite (kuulokkeet + BlackHole) ja valitse se ulostuloksi."

step "Asetukset"
"$PY" -m meetnotes.winsetup config

step "Puheentunnistusmalli"
"$PY" -m meetnotes.winsetup model || echo "Mallin lataus ei onnistunut nyt; se ladataan ensimmäisen kokouksen alussa."

step "Meetnotes.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$HOME/Applications"
cat > "$APP/Contents/MacOS/Meetnotes" <<LAUNCH
#!/bin/bash
exec "$PY" -m meetnotes.desktop
LAUNCH
chmod +x "$APP/Contents/MacOS/Meetnotes"
cp "$SOURCE/meetnotes/web/meetnotes.icns" "$APP/Contents/Resources/meetnotes.icns" 2>/dev/null || true
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Meetnotes</string>
  <key>CFBundleDisplayName</key><string>Meetnotes</string>
  <key>CFBundleIdentifier</key><string>com.meetnotes.app</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Meetnotes</string>
  <key>CFBundleIconFile</key><string>meetnotes</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Meetnotes nauhoittaa kokouksesi ääntä muistiinpanoja varten.</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
echo "Luotu $APP"

step "Käynnistys kirjautuessa"
"$PY" -m meetnotes.cli autostart on

step "Avataan Meetnotes"
open "$APP"

echo
printf '\033[32mValmis. Meetnotes löytyy kansiosta ~/Applications (vedä se Dockiin).\033[0m\n'
echo "Anna macOS:n kysyessä mikrofoni- ja käyttöapuoikeudet (Järjestelmäasetukset > Tietosuoja ja suojaus)."
echo "Lisää sovelluksessa Asetukset-sivulla Claude API -avain."
read -r -p "Paina Enter sulkeaksesi." _
