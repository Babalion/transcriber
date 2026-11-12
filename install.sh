#!/usr/bin/env bash
# Install transcriber for the current user: command, desktop entry, config, shortcut.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/transcriber"
BINDING="${1:-<Control><Alt>d}"

SCHEMA=org.gnome.settings-daemon.plugins.media-keys
KB_PATH=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/transcriber/

echo "→ Checking dependencies"
missing=()
command -v wl-copy >/dev/null || missing+=("wl-clipboard")
command -v notify-send >/dev/null || missing+=("libnotify")
/usr/bin/python3 - <<'EOF' || missing+=("python3-gobject gtk4 libadwaita gstreamer1-plugins-good")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Adw, Gst
Gst.init(None)
assert Gst.ElementFactory.find("level") and Gst.ElementFactory.find("flacenc")
EOF
if ((${#missing[@]})); then
    echo "  Missing packages: ${missing[*]}"
    echo "  Fedora: sudo dnf install ${missing[*]}"
    exit 1
fi

echo "→ Linking $BIN_DIR/transcriber"
mkdir -p "$BIN_DIR"
chmod +x "$REPO/transcriber.py"
ln -sf "$REPO/transcriber.py" "$BIN_DIR/transcriber"

echo "→ Installing desktop entry"
mkdir -p "$APP_DIR"
sed "s|@EXEC@|$BIN_DIR/transcriber|" "$REPO/io.github.babalion.Transcriber.desktop.in" \
    > "$APP_DIR/io.github.babalion.Transcriber.desktop"

echo "→ Config in $CFG_DIR"
mkdir -p "$CFG_DIR"
[[ -e "$CFG_DIR/config.toml" ]] || cp "$REPO/config.example.toml" "$CFG_DIR/config.toml"
[[ -e "$CFG_DIR/vocab.txt" ]] || cp "$REPO/vocab.example.txt" "$CFG_DIR/vocab.txt"

if command -v gsettings >/dev/null && gsettings list-schemas | grep -qx "$SCHEMA"; then
    echo "→ Keyboard shortcut $BINDING"
    current="$(gsettings get "$SCHEMA" custom-keybindings)"
    new="$(/usr/bin/python3 -c '
import ast, sys
cur = sys.argv[1].removeprefix("@as ")
paths = ast.literal_eval(cur) if cur.strip() else []
if sys.argv[2] not in paths:
    paths.append(sys.argv[2])
print(paths)' "$current" "$KB_PATH")"
    gsettings set "$SCHEMA" custom-keybindings "$new"
    gsettings set "$SCHEMA.custom-keybinding:$KB_PATH" name "Transcriber"
    gsettings set "$SCHEMA.custom-keybinding:$KB_PATH" command "$BIN_DIR/transcriber"
    gsettings set "$SCHEMA.custom-keybinding:$KB_PATH" binding "$BINDING"
else
    echo "  GNOME not detected - bind '$BIN_DIR/transcriber' to a key yourself."
fi

echo
echo "Done. Put your key into ~/.env as GROQ_API_KEY=gsk_... and press $BINDING."
