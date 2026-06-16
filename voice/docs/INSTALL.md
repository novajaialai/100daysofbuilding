# Set up "AI Pilled Voice" — Super+Z push-to-talk dictation

**Hand this whole file to Claude Code on the target laptop and say: "set this up for me."**
It's a self-contained runbook — every file's contents and every command is here.

## What it builds

Press **Super+Z** → a beep, "Listening…" → talk → press **Super+Z** again → it
transcribes locally and **types the text into whatever window is focused, then
hits Enter**. Fully offline STT (faster-whisper on CPU), no API keys, no cloud.

It's a GNOME custom keybinding that fires one toggle script:

```
Super+Z ─▶ ~/voice/voice.sh
              ├─ press 1: start recording (pw-record / parecord / arecord) → a .wav
              └─ press 2: stop → transcribe.py (faster-whisper) → ydotool types it + Enter
```

`ydotool` injects keystrokes through `/dev/uinput` (root-only), so a tiny root
daemon `ydotoold` runs as a system service; everything else is per-user.

---

## Assumptions — Claude, check these FIRST and tell the user if any differ

- **Linux with the GNOME desktop** (Wayland or X11). Verified on Fedora and
  **Ubuntu** (both default to GNOME). The hotkey uses GNOME's custom-shortcut
  system. On KDE/other DEs, everything works *except* step 5 — bind the key in
  that DE's settings instead.
- **Ubuntu version matters for `ydotool`** (see the callout in Step 4):
  **24.04 LTS** ships ydotool 1.0.4 and works verbatim; **22.04 LTS** ships the
  old 0.x with a different socket model and needs the fallback. Prefer 24.04.
- A working microphone and a **PipeWire or PulseAudio** audio stack (Fedora/
  Ubuntu defaults). 
- Python 3.9+.
- One **sudo** step (installing the `ydotoold` service). Everything else is
  user-level. Flag that step for the user to approve.

Run this to report the environment before proceeding:

```bash
echo "Desktop: $XDG_CURRENT_DESKTOP / $XDG_SESSION_TYPE"
. /etc/os-release 2>/dev/null; echo "Distro: $NAME $VERSION_ID"
echo "User: $(whoami)  UID:$(id -u)  GID:$(id -g)  HOME:$HOME"
command -v dnf apt-get pacman 2>/dev/null
```

If it's not Linux+GNOME, stop and tell the user — this runbook assumes both.

---

## Step 1 — Install system packages

Pick the block matching the distro.

**Fedora / RHEL:**
```bash
sudo dnf install -y ydotool pipewire-utils pulseaudio-utils alsa-utils libnotify python3-pip
```

**Debian / Ubuntu:**
```bash
sudo apt update && sudo apt install -y ydotool pipewire-bin pulseaudio-utils alsa-utils libnotify-bin python3-pip python3-venv
```

(These provide: `ydotool`+`ydotoold`, a recorder `pw-record`/`parecord`/`arecord`,
`paplay`+`pactl` for beeps/mic config, `notify-send` for toasts.)

## Step 2 — Install faster-whisper (local STT) in a venv

A venv avoids "externally-managed environment" errors on newer distros and keeps
it self-contained.

```bash
mkdir -p ~/voice
python3 -m venv ~/voice/.venv
~/voice/.venv/bin/pip install -U pip faster-whisper
```

## Step 3 — Create the voice scripts

Create these two files exactly.

### `~/voice/transcribe.py`

```python
#!/usr/bin/env python3
"""AI Pilled Voice — transcribe a wav to one line of text (faster-whisper, CPU, local)."""
import sys
from faster_whisper import WhisperModel

wav = sys.argv[1]
name = sys.argv[2] if len(sys.argv) > 2 else "base"

# int8 on CPU: ~1-2s for short clips with the base model, fully offline.
model = WhisperModel(name, device="cpu", compute_type="int8")
segments, _info = model.transcribe(wav, language="en", vad_filter=True, beam_size=1)

text = " ".join(seg.text.strip() for seg in segments)
text = " ".join(text.split())  # collapse whitespace to single spaces
sys.stdout.write(text)
```

### `~/voice/voice.sh`

```bash
#!/usr/bin/env bash
# AI Pilled Voice — push-to-talk dictation into the focused window.
# Toggle: first press starts recording; second press transcribes + types it + Enter.
# Bound to Super+Z via a GNOME custom keybinding.
set -uo pipefail

DIR="$HOME/voice"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
WAV="$RUNTIME/voice-rec.wav"
PIDFILE="$RUNTIME/voice-rec.pid"
LOG="$DIR/voice.log"

# --- config (override via env) ---------------------------------------------
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-/run/ydotoold.sock}"
MODEL="${VOICE_MODEL:-base}"          # base | small | small.en | medium ...
AUTO_ENTER="${VOICE_AUTO_ENTER:-1}"   # 1 = submit automatically, 0 = leave text for review
# Prefer the project venv's python, fall back to system python3.
PY="$DIR/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
# ---------------------------------------------------------------------------

SND=/usr/share/sounds/freedesktop/stereo
beep_start(){ paplay "$SND/dialog-information.oga" >/dev/null 2>&1 & }
beep_stop(){  paplay "$SND/message.oga"            >/dev/null 2>&1 & }
beep_done(){  paplay "$SND/complete.oga"           >/dev/null 2>&1 & }
note(){ notify-send -t 2500 -a "Voice" "$1" "${2:-}" >/dev/null 2>&1 || true; }

# Pick whatever recorder exists; all write a real .wav at 16k mono s16.
rec_cmd(){
  if   command -v pw-record >/dev/null 2>&1; then echo "pw-record --rate 16000 --channels 1 --format s16";
  elif command -v parecord  >/dev/null 2>&1; then echo "parecord --rate=16000 --channels=1 --file-format=wav";
  elif command -v arecord   >/dev/null 2>&1; then echo "arecord -q -t wav -f S16_LE -r 16000 -c 1";
  else echo ""; fi
}

recording(){ [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

if recording; then
  # ---- STOP: finalize recording, transcribe, inject ----
  PID="$(cat "$PIDFILE")"; rm -f "$PIDFILE"
  kill "$PID" 2>/dev/null
  for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.05; done
  beep_stop; note "Transcribing…"

  TEXT="$("$PY" "$DIR/transcribe.py" "$WAV" "$MODEL" 2>>"$LOG")"
  TEXT="${TEXT#"${TEXT%%[![:space:]]*}"}"   # ltrim
  TEXT="${TEXT%"${TEXT##*[![:space:]]}"}"   # rtrim

  if [[ -z "$TEXT" ]]; then note "Heard nothing" "No speech detected."; exit 0; fi

  if ! ydotool type "$TEXT" 2>>"$LOG"; then
    note "ydotool failed" "Is ydotoold running? Check voice.log"; exit 1
  fi
  if [[ "$AUTO_ENTER" == "1" ]]; then sleep 0.12; ydotool key 28:1 28:0 2>>"$LOG"; fi
  beep_done; note "Sent ✓" "$TEXT"
else
  # ---- START: detach a recorder that survives this script exiting ----
  REC="$(rec_cmd)"
  if [[ -z "$REC" ]]; then note "No recorder" "Install pipewire-utils or alsa-utils"; exit 1; fi
  rm -f "$WAV"
  setsid $REC "$WAV" >/dev/null 2>/dev/null &
  echo $! > "$PIDFILE"
  beep_start; note "Listening…" "Super+Z again to send"
fi
```

Make the scripts executable:

```bash
chmod +x ~/voice/voice.sh ~/voice/transcribe.py
```

## Step 4 — Install the `ydotoold` root daemon  ⚠ needs sudo

`ydotool` types via `/dev/uinput` (root-only), so its daemon runs as a system
service. This generator stamps the **current user's** uid/gid into the service
so the socket is usable without root — do **not** hardcode 1000.

> **⚠ Check the ydotool version first (Ubuntu 22.04 caveat):**
> ```bash
> ydotoold --help 2>&1 | grep -q -- '-p' && echo "modern (1.0+) — use Step 4 as written" || echo "OLD 0.x — use the fallback below"
> ```
> **Modern (1.0+, incl. Fedora and Ubuntu 24.04):** proceed with Step 4 exactly.
>
> **Old 0.x (Ubuntu 22.04's apt package):** its daemon ignores `-p/-P/-o` and
> puts the socket at `$XDG_RUNTIME_DIR/.ydotool_socket`, so the root-service trick
> doesn't grant the user access. Easiest fixes, in order:
> 1. **Best:** use Ubuntu 24.04, or otherwise get ydotool ≥1.0 (then Step 4 works).
> 2. **Or** run it as the user with a uinput group + udev rule:
>    ```bash
>    sudo groupadd -f uinput && sudo usermod -aG uinput "$USER"
>    echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | sudo tee /etc/udev/rules.d/80-uinput.rules
>    sudo udevadm control --reload-rules && sudo modprobe uinput
>    # log out/in for the group to take effect, then run ydotoold as a user service:
>    systemctl --user enable --now ydotoold  2>/dev/null || (setsid ydotoold &)
>    ```
>    and set `export YDOTOOL_SOCKET="$XDG_RUNTIME_DIR/.ydotool_socket"` at the top
>    of `voice.sh` (replacing the `/run/ydotoold.sock` default). Then **skip the
>    rest of Step 4.**

Save `~/voice/setup-ydotoold.sh`:

```bash
#!/usr/bin/env bash
# One-time root setup for ydotoold. Run with: sudo bash ~/voice/setup-ydotoold.sh
set -e
# the real user (works whether invoked via sudo or as root for a target user)
RUID="${SUDO_UID:-$(id -u)}"; RGID="${SUDO_GID:-$(id -g)}"
YD="$(command -v ydotoold || echo /usr/bin/ydotoold)"

cat > /etc/systemd/system/ydotoold.service <<EOF
[Unit]
Description=ydotoold — virtual input daemon for ydotool
After=systemd-user-sessions.service

[Service]
Type=simple
ExecStartPre=-/usr/sbin/modprobe uinput
ExecStart=$YD -p /run/ydotoold.sock -P 0660 -o $RUID:$RGID
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

modprobe uinput || true
systemctl daemon-reload
systemctl enable --now ydotoold.service
sleep 1
systemctl --no-pager --full status ydotoold.service | head -n 8
ls -l /run/ydotoold.sock
echo "Done — socket owned by $RUID:$RGID at /run/ydotoold.sock"
```

Then run it (this is the only sudo step — have the user approve it):

```bash
sudo bash ~/voice/setup-ydotoold.sh
```

## Step 5 — Bind Super+Z in GNOME

This appends a custom shortcut without clobbering existing ones, and points it
at the user's own `~/voice/voice.sh`:

```bash
SCHEMA=org.gnome.settings-daemon.plugins.media-keys
NEWPATH=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice/
cur=$(gsettings get $SCHEMA custom-keybindings)
case "$cur" in
  *"$NEWPATH"*) : ;;                                   # already present
  "@as []"|"[]") gsettings set $SCHEMA custom-keybindings "['$NEWPATH']" ;;
  *)            gsettings set $SCHEMA custom-keybindings "${cur%]}, '$NEWPATH']" ;;
esac
kb=$SCHEMA.custom-keybinding:$NEWPATH
gsettings set $kb name 'AI Pilled Voice'
gsettings set $kb command "$HOME/voice/voice.sh"
gsettings set $kb binding '<Super>z'
echo "Bound Super+Z → $HOME/voice/voice.sh"
```

> If `<Super>z` clashes with something the user relies on, pick another binding
> (e.g. `<Super>grave` or `<Control><Alt>v`).

## Step 6 — Verify

```bash
# 1) daemon up + socket present?
systemctl status ydotoold --no-pager | head -3 && ls -l /run/ydotoold.sock

# 2) typing works? (focus a text field within 2s)
( sleep 2; YDOTOOL_SOCKET=/run/ydotoold.sock ydotool type "ydotool ok" )

# 3) STT works? record 3s, transcribe (warms up + downloads the ~140MB base model once)
REC="$(command -v pw-record || command -v parecord || command -v arecord)"; echo "recorder: $REC"
timeout 3 pw-record --rate 16000 --channels 1 --format s16 /tmp/t.wav 2>/dev/null || \
  timeout 3 parecord --rate=16000 --channels=1 --file-format=wav /tmp/t.wav 2>/dev/null || \
  timeout 3 arecord -q -t wav -f S16_LE -r 16000 -c 1 /tmp/t.wav
~/voice/.venv/bin/python ~/voice/transcribe.py /tmp/t.wav base; echo

# 4) end-to-end: press Super+Z, say a sentence, press Super+Z again — it types into the focused window.
```

First Super+Z transcription is slow (it downloads the whisper model once, ~140MB,
cached under `~/.cache/huggingface`). After that it's ~1–2s.

---

## Tuning

- **Don't auto-submit** (type text but let the user hit Enter): set
  `VOICE_AUTO_ENTER=0` in `voice.sh`, or in the GNOME shortcut command use
  `env VOICE_AUTO_ENTER=0 ~/voice/voice.sh`.
- **More accurate** (slower, ~480MB): `VOICE_MODEL=small.en`.
- **Change the hotkey:** GNOME Settings → Keyboard → View and Customize Shortcuts
  → Custom Shortcuts → "AI Pilled Voice".

## Troubleshooting

- **Nothing types** → `systemctl status ydotoold`; confirm `/run/ydotoold.sock`
  exists and is owned by the user's uid. Re-run step 4 if not.
- **`ydotool: failed to connect socket`** → the service's `-o uid:gid` doesn't
  match the user. Re-run `sudo bash ~/voice/setup-ydotoold.sh` *as that user's
  sudo* (it reads `$SUDO_UID`).
- **Recording is silent / wrong mic** → `pactl info | grep "Default Source"`,
  then `pactl set-default-source <name>`. List sources: `pactl list short sources`.
- **Errors** → everything logs to `~/voice/voice.log`.
- **No beeps** → cosmetic; needs the `sound-theme-freedesktop` package. Safe to ignore.
- **Hotkey doesn't fire** → confirm GNOME (`echo $XDG_CURRENT_DESKTOP`); on other
  desktops bind `~/voice/voice.sh` to a key in that DE instead.

## Files this creates

```
~/voice/
├─ voice.sh             # the Super+Z toggle (recorder-agnostic, venv-aware)
├─ transcribe.py        # faster-whisper, CPU, base model, English
├─ setup-ydotoold.sh    # one-time root daemon install (uid/gid-correct)
├─ .venv/               # faster-whisper lives here
└─ voice.log            # errors
# + a GNOME custom shortcut "AI Pilled Voice" → <Super>z
# + /etc/systemd/system/ydotoold.service (system service)
```

Built for Jake's "AI Pilled" setup; adapted here to be portable across users and
machines (no hardcoded home dir or uid).
