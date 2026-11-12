#!/usr/bin/env bash
# Remove what install.sh set up. Your config in ~/.config/transcriber is kept.
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SCHEMA=org.gnome.settings-daemon.plugins.media-keys
KB_PATH=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/transcriber/

rm -f "$BIN_DIR/transcriber" "$APP_DIR/io.github.babalion.Transcriber.desktop"

if command -v gsettings >/dev/null && gsettings list-schemas | grep -qx "$SCHEMA"; then
    current="$(gsettings get "$SCHEMA" custom-keybindings)"
    new="$(/usr/bin/python3 -c '
import ast, sys
cur = sys.argv[1].removeprefix("@as ")
paths = ast.literal_eval(cur) if cur.strip() else []
print([p for p in paths if p != sys.argv[2]])' "$current" "$KB_PATH")"
    gsettings set "$SCHEMA" custom-keybindings "$new"
    gsettings reset-recursively "$SCHEMA.custom-keybinding:$KB_PATH" 2>/dev/null || true
fi

echo "Removed. Config left in ${XDG_CONFIG_HOME:-$HOME/.config}/transcriber."
