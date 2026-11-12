#!/usr/bin/python3
# transcriber - push-to-talk dictation for GNOME/Wayland using the Groq Whisper API
# Copyright (C) 2026 Chris Nill
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file for details.
"""Record from the microphone, transcribe with Groq, put the text on the clipboard.

Start it (e.g. via a keyboard shortcut) to open a small recording window.
Starting it again while the window is open finishes the recording.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, GLib, Gst, Gtk  # noqa: E402

APP_ID = "io.github.babalion.Transcriber"
API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "transcriber"

DEFAULTS = {
    "language": "de",
    "model": "whisper-large-v3",
    "env_file": "~/.env",
    "vocab_file": str(CONFIG_DIR / "vocab.txt"),
    "prompt_prefix": "Fachbegriffe:",
    "close_delay_ms": 700,
}

# Whisper only looks at the last ~224 tokens of the prompt; stay well below.
MAX_PROMPT_CHARS = 600
WAVE_BARS = 64
# Peak level (dBFS) a recording must reach at least once to be sent at all.
SILENCE_DB = -45.0


# --------------------------------------------------------------------------- config


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = CONFIG_DIR / "config.toml"
    if path.exists():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def read_api_key(cfg: dict) -> str | None:
    """GROQ_API_KEY from the environment, else that single line from env_file."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(cfg["env_file"]).expanduser()
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if line.startswith("GROQ_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def build_prompt(cfg: dict) -> str:
    path = Path(cfg["vocab_file"]).expanduser()
    if not path.exists():
        return ""
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.extend(t.strip() for t in line.split(",") if t.strip())
    if not terms:
        return ""
    prompt = f"{cfg['prompt_prefix']} " + ", ".join(terms) + "."
    return prompt[:MAX_PROMPT_CHARS]


# --------------------------------------------------------------------------- Groq


class TranscriptionError(Exception):
    pass


def transcribe(audio: Path, cfg: dict) -> str:
    key = read_api_key(cfg)
    if not key:
        raise TranscriptionError(
            f"Kein GROQ_API_KEY gefunden (Umgebung oder {cfg['env_file']})."
        )

    fields = {
        "model": cfg["model"],
        "response_format": "json",
        "temperature": "0",
    }
    if cfg.get("language"):
        fields["language"] = cfg["language"]
    prompt = build_prompt(cfg)
    if prompt:
        fields["prompt"] = prompt

    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
        "Content-Type: audio/flac\r\n\r\n"
    ).encode()
    body += audio.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        API_URL,
        data=bytes(body),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "transcriber/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = detail[:200]
        raise TranscriptionError(f"Groq-Fehler {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise TranscriptionError(f"Keine Verbindung zu Groq: {reason}") from e
    return data.get("text", "").strip()


def copy_to_clipboard(text: str) -> None:
    # wl-copy forks and keeps serving the clipboard after we exit; GTK's own
    # clipboard would be lost as soon as this process quits.
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy"], input=text.encode(), check=True, timeout=5)
    else:
        raise TranscriptionError("wl-copy nicht gefunden (Paket wl-clipboard).")


def notify(summary: str, body: str = "") -> None:
    if shutil.which("notify-send"):
        subprocess.Popen(
            ["notify-send", "-a", "Transcriber", "-i", "audio-input-microphone-symbolic",
             "-e", summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


# --------------------------------------------------------------------------- recorder


class Recorder:
    """GStreamer pipeline: mic -> 16 kHz mono -> level meter -> valve -> FLAC file."""

    def __init__(self, path: Path, on_level, on_error):
        self.path = path
        self.on_level = on_level
        self.on_error = on_error
        self._eos_callback = None
        self._stopped = False
        self.pipeline = Gst.parse_launch(
            "pulsesrc ! audioconvert ! audioresample "
            "! audio/x-raw,rate=16000,channels=1 "
            "! level name=level interval=40000000 post-messages=true "
            "! valve name=valve drop=false "
            "! flacenc ! filesink name=sink"
        )
        self.pipeline.get_by_name("sink").set_property("location", str(path))
        self.valve = self.pipeline.get_by_name("valve")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.on_error("Mikrofon konnte nicht geöffnet werden.")

    def set_paused(self, paused: bool):
        self.valve.set_property("drop", paused)

    def finish(self, callback):
        """Flush and close the file, then call callback()."""
        self.set_paused(False)
        self._eos_callback = callback
        self.pipeline.send_event(Gst.Event.new_eos())

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_signal_watch()

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s and s.get_name() == "level":
                peaks = s.get_value("peak")
                if peaks:
                    self.on_level(max(peaks))
        elif msg.type == Gst.MessageType.EOS:
            self.stop()
            if self._eos_callback:
                cb, self._eos_callback = self._eos_callback, None
                cb()
        elif msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            self.stop()
            self.on_error(f"Aufnahmefehler: {err.message}")


# --------------------------------------------------------------------------- UI


CSS = """
.transcriber-window { border-radius: 16px; }
.key-hint { font-size: 0.75em; opacity: 0.6; }
.timer { font-feature-settings: "tnum"; font-weight: bold; }
.status { font-size: 0.9em; }
"""


class Waveform(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.levels = deque([0.0] * WAVE_BARS, maxlen=WAVE_BARS)
        self.dimmed = False
        self.set_hexpand(True)
        self.set_content_height(56)
        self.set_draw_func(self._draw)

    def push(self, db: float):
        # map -60 dB .. 0 dB to 0 .. 1, slightly compressed so speech looks lively
        norm = min(max((db + 60.0) / 60.0, 0.0), 1.0) ** 1.6
        self.levels.append(norm)
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        color = Adw.StyleManager.get_default().get_accent_color_rgba()
        alpha = 0.35 if self.dimmed else 1.0
        cr.set_source_rgba(color.red, color.green, color.blue, alpha)
        step = width / WAVE_BARS
        bar_w = max(step * 0.6, 1.5)
        mid = height / 2
        for i, lvl in enumerate(self.levels):
            h = max(lvl * (height - 4), 2.0)
            x = i * step + (step - bar_w) / 2
            radius = bar_w / 2
            y = mid - h / 2
            # rounded bar
            cr.new_sub_path()
            cr.arc(x + radius, y + radius, radius, 3.1416, 0)
            cr.arc(x + radius, y + h - radius, radius, 0, 3.1416)
            cr.close_path()
            cr.fill()


def button_with_hint(icon: str, tooltip: str, hint: str, css: str | None = None):
    btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    btn.add_css_class("circular")
    if css:
        btn.add_css_class(css)
    btn.set_focusable(False)  # keys are handled by the window, not by focus
    btn.set_halign(Gtk.Align.CENTER)
    label = Gtk.Label(label=hint)
    label.add_css_class("key-hint")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.append(btn)
    box.append(label)
    return box, btn


class TranscriberWindow(Adw.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title="Transcriber")
        self.cfg = cfg
        self.state = "recording"  # recording | paused | transcribing | error | done
        self.elapsed = 0.0
        self._last_tick = time.monotonic()

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(460, -1)
        self.add_css_class("transcriber-window")

        tmp_root = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        self.tmpdir = Path(tempfile.mkdtemp(prefix="transcriber-", dir=tmp_root))
        self.audio = self.tmpdir / "recording.flac"

        # --- widgets
        self.wave = Waveform()
        self.timer = Gtk.Label(label="0:00")
        self.timer.add_css_class("timer")
        self.status = Gtk.Label(label="Aufnahme läuft", xalign=0)
        self.status.add_css_class("status")
        self.status.set_wrap(True)
        self.status.set_max_width_chars(40)
        self.spinner = Adw.Spinner()
        self.spinner.set_visible(False)

        pause_box, self.pause_btn = button_with_hint(
            "media-playback-pause-symbolic", "Pausieren", "Leertaste"
        )
        cancel_box, self.cancel_btn = button_with_hint(
            "window-close-symbolic", "Abbrechen", "Esc", "destructive-action"
        )
        finish_box, self.finish_btn = button_with_hint(
            "object-select-symbolic", "Abschließen", "Enter", "suggested-action"
        )
        self.pause_btn.connect("clicked", lambda *_: self.toggle_pause())
        self.cancel_btn.connect("clicked", lambda *_: self.cancel())
        self.finish_btn.connect("clicked", lambda *_: self.finish())

        top = Gtk.Box(spacing=8)
        top.append(self.spinner)
        top.append(self.status)
        self.status.set_hexpand(True)
        top.append(self.timer)

        buttons = Gtk.Box(spacing=10)
        for b in (pause_box, cancel_box, finish_box):
            buttons.append(b)

        row = Gtk.Box(spacing=14)
        row.append(self.wave)
        row.append(buttons)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(14)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.append(top)
        content.append(row)

        handle = Gtk.WindowHandle()  # lets you drag the undecorated window
        handle.set_child(content)
        self.set_content(handle)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("close-request", self._on_close_request)

        # --- recording
        self.max_db = -100.0
        self.recorder = Recorder(self.audio, self._on_level, self.show_error)
        self.recorder.start()
        GLib.timeout_add(200, self._tick)

    def _on_level(self, db):
        if self.state == "recording":
            self.max_db = max(self.max_db, db)
        self.wave.push(db)

    # ---------------------------------------------------------------- actions

    def _on_key(self, _ctrl, keyval, _code, _mods):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.finish()
        elif keyval == Gdk.KEY_Escape:
            self.cancel()
        elif keyval == Gdk.KEY_space:
            self.toggle_pause()
        else:
            return False
        return True

    def toggle_pause(self):
        if self.state == "recording":
            self.state = "paused"
            self.recorder.set_paused(True)
            self.pause_btn.set_icon_name("media-record-symbolic")
            self.pause_btn.set_tooltip_text("Fortsetzen")
            self.status.set_label("Pausiert")
            self.wave.dimmed = True
            self.wave.queue_draw()
        elif self.state == "paused":
            self.state = "recording"
            self.recorder.set_paused(False)
            self.pause_btn.set_icon_name("media-playback-pause-symbolic")
            self.pause_btn.set_tooltip_text("Pausieren")
            self.status.set_label("Aufnahme läuft")
            self.wave.dimmed = False
            self.wave.queue_draw()

    def finish(self):
        if self.state in ("recording", "paused"):
            self._set_busy("Transkribiere …")
            self.recorder.finish(self._start_transcription)
        elif self.state == "error" and self.audio.exists():
            self._set_busy("Sende erneut …")
            self._start_transcription()

    def cancel(self):
        if self.state != "transcribing":
            self.close()

    # ---------------------------------------------------------------- transcription

    def _set_busy(self, text):
        self.state = "transcribing"
        self.status.set_label(text)
        self.spinner.set_visible(True)
        self.wave.dimmed = True
        self.wave.queue_draw()
        for b in (self.pause_btn, self.cancel_btn, self.finish_btn):
            b.set_sensitive(False)

    def _start_transcription(self):
        def work():
            try:
                if not self.audio.exists() or self.audio.stat().st_size < 1000:
                    raise TranscriptionError("Aufnahme ist leer.")
                if self.max_db < SILENCE_DB:
                    raise TranscriptionError("Nur Stille aufgenommen – Mikrofon stumm?")
                text = transcribe(self.audio, self.cfg)
                # Whisper hallucinates symbols like "●" or "…" on near-silence
                if not any(c.isalnum() for c in text):
                    raise TranscriptionError("Kein Text erkannt.")
                copy_to_clipboard(text)
            except TranscriptionError as e:
                GLib.idle_add(self.show_error, str(e))
            except Exception as e:  # noqa: BLE001 - surface anything in the UI
                GLib.idle_add(self.show_error, f"Unerwarteter Fehler: {e}")
            else:
                GLib.idle_add(self._done, text)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text):
        self.state = "done"
        self.spinner.set_visible(False)
        self.status.set_label("✓ In der Zwischenablage – Strg+V zum Einfügen")
        preview = text if len(text) <= 120 else text[:117] + "…"
        notify("📋 In der Zwischenablage", preview)
        GLib.timeout_add(int(self.cfg["close_delay_ms"]), self._close_now)
        return False

    def show_error(self, message):
        recording_lost = self.state in ("recording", "paused")
        self.state = "error"
        self.spinner.set_visible(False)
        self.status.set_label(f"⚠ {message}")
        self.pause_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(True)
        can_retry = not recording_lost and self.audio.exists()
        self.finish_btn.set_sensitive(can_retry)
        self.finish_btn.set_icon_name("view-refresh-symbolic")
        self.finish_btn.set_tooltip_text("Erneut senden")
        return False

    # ---------------------------------------------------------------- lifecycle

    def _tick(self):
        now = time.monotonic()
        if self.state == "recording":
            self.elapsed += now - self._last_tick
        self._last_tick = now
        s = int(self.elapsed)
        self.timer.set_label(f"{s // 60}:{s % 60:02d}")
        return self.state in ("recording", "paused")

    def _close_now(self):
        self.close()
        return False

    def _on_close_request(self, _win):
        if self.state == "transcribing":
            return True  # don't lose the request half-way
        self.cleanup()
        return False

    def cleanup(self):
        try:
            self.recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TranscriberApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)

    def do_activate(self):
        if self.window is not None:
            # Second launch (shortcut pressed again): finish the recording.
            self.window.finish()
            return
        self.window = TranscriberWindow(self, load_config())
        self.window.connect("destroy", self._on_window_destroy)
        self.window.present()

    def _on_window_destroy(self, _win):
        self.window = None

    def _on_signal(self):
        if self.window is not None:
            self.window.cleanup()
        self.quit()
        return GLib.SOURCE_REMOVE


def main():
    Gst.init(None)
    return TranscriberApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
