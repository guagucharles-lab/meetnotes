#!/bin/bash
# Meetnotes: poisto macOS:ltä. Muistiinpanot (~/MeetingNotes) jäävät talteen.
APPDIR="$HOME/Library/Application Support/meetnotes"
PY="$APPDIR/venv/bin/python3"
echo "Meetnotes - poisto"
if [ -x "$PY" ]; then
  "$PY" -m meetnotes.cli stop || true
  "$PY" -m meetnotes.cli autostart off || true
  "$PY" -c "import keyring; keyring.delete_password('meetnotes', 'anthropic_api_key')" 2>/dev/null || true
fi
pkill -f "meetnotes.desktop" 2>/dev/null || true
rm -rf "$HOME/Applications/Meetnotes.app"
[ -n "$APPDIR" ] && rm -rf "$APPDIR"
rm -rf "$HOME/.config/meetnotes" "$HOME/.local/state/meetnotes"
echo "Meetnotes poistettu. Muistiinpanosi ovat yhä tallessa: $HOME/MeetingNotes"
read -r -p "Paina Enter sulkeaksesi." _
