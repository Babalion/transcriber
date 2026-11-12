# transcriber

Push-to-talk dictation for GNOME on Wayland. Press a shortcut, speak, press
Enter – the transcript lands on your clipboard, ready for `Ctrl+V`.
Speech recognition runs on [Groq](https://groq.com)'s hosted Whisper models,
which usually answer in well under a second.

```
┌──────────────────────────────────────────────────────────────┐
│ Aufnahme läuft                                          0:07 │
│ ▁▂▅▇▆▃▂▁▁▃▆▇█▆▄▂▁▁▂▄▆▅▃▁        (⏸)     (✕)     (✓)          │
│                                Space    Esc     Enter        │
└──────────────────────────────────────────────────────────────┘
```

## Features

- Small undecorated window with a live waveform and a timer
- **Pause** (`Space`), **cancel** (`Esc`), **finish** (`Enter`, or press the
  shortcut again)
- Vocabulary prompt so Whisper spells your technical terms correctly
- On network or API errors the window stays open with a **retry** button, so a
  long dictation is not lost
- Audio is kept only in `$XDG_RUNTIME_DIR` (RAM, user-only) and deleted when the
  window closes
- No root access, no daemon, no extra Python packages – just GTK4, libadwaita and
  GStreamer from your distribution

## Why the clipboard and not typing directly?

GNOME's compositor does not implement the Wayland *virtual keyboard* protocol,
so tools like `wtype` do not work there. The remaining options all have a cost:

| Approach | Downside |
| --- | --- |
| `ydotool` / `dotool` via `/dev/uinput` | needs a udev rule and group membership; any process of your user could then inject keystrokes; typing non-ASCII characters (ä, ö, ß, …) breaks with non-US layouts |
| RemoteDesktop portal | permission dialog, more moving parts |
| GNOME Shell extension | breaks easily with GNOME updates |
| IBus input method | considerably more complex |

Copying to the clipboard (`wl-copy`) works everywhere, keeps every character
intact and needs no extra permissions. You press `Ctrl+V` (or `Ctrl+Shift+V` in
a terminal) yourself. Automatic pasting may come later as an opt-in.

## Requirements

- GNOME on Wayland (other desktops work too; bind the command to a key yourself)
- Python ≥ 3.11 with PyGObject, GTK 4, libadwaita ≥ 1.6
- GStreamer with the *good* plugins (`level`, `flacenc`, `pulsesrc`)
- `wl-clipboard`, `libnotify`
- A Groq API key – <https://console.groq.com/keys>

On Fedora all of this is preinstalled with Workstation except possibly:

```bash
sudo dnf install wl-clipboard python3-gobject libadwaita gstreamer1-plugins-good
```

## Install

```bash
git clone https://github.com/Babalion/transcriber.git
cd transcriber
./install.sh                 # default shortcut: Ctrl+Alt+D
./install.sh '<Super>y'      # or choose your own
```

This links `~/.local/bin/transcriber`, adds a desktop entry, copies the example
config to `~/.config/transcriber/` and registers the GNOME shortcut. Updating is
just `git pull`. `./uninstall.sh` reverts it (your config is kept).

Then put your API key into `~/.env`:

```bash
GROQ_API_KEY=gsk_...
```

Only that one line is read from the file. A `GROQ_API_KEY` environment variable
takes precedence. Keep the file private: `chmod 600 ~/.env`.

## Configuration

`~/.config/transcriber/config.toml` – see [config.example.toml](config.example.toml):

| Key | Default | |
| --- | --- | --- |
| `language` | `"de"` | ISO-639-1 code, `""` for auto-detect |
| `model` | `"whisper-large-v3"` | or `"whisper-large-v3-turbo"` |
| `env_file` | `"~/.env"` | where to look for `GROQ_API_KEY` |
| `vocab_file` | `"~/.config/transcriber/vocab.txt"` | terms for the prompt |
| `prompt_prefix` | `"Fachbegriffe:"` | text before the vocabulary |
| `close_delay_ms` | `700` | how long the success message stays |

`vocab.txt` holds comma- or line-separated terms (`#` for comments). Whisper only
reads about 200 tokens of prompt, so keep it focused; the list is cut at 600
characters.

## Privacy

Your recording is sent to Groq for transcription; see their privacy policy. The
local copy lives in `$XDG_RUNTIME_DIR` and is removed when the window closes.

## License

[GPL-3.0-or-later](LICENSE)
