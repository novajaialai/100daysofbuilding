# AI Pilled Voice — talk to your computer

Local, offline push-to-talk dictation for Linux/GNOME. Hold a thought, tap a
hotkey, talk, tap again — the words are transcribed **on your machine** and typed
into whatever window is focused. No API keys, no cloud, no account.

Built to talk to [Claude Code](https://claude.com/claude-code) in the terminal,
but it types into any app.

```
 ┌─ Super+Z ─▶ voice.sh ──────────────┐   terminal + auto-Enter
 │            press 1 → 🎙  record     │   (built for Claude Code:
 │            press 2 → transcribe →   │    speak, it runs)
 │                      type + ⏎       │
 └─────────────────────────────────────┘

 ┌─ Super+A ─▶ voice-anywhere.sh ─────┐   any field, NO Enter
 │            press 1 → 🎙  record     │   (chat boxes, docs, search
 │            press 2 → transcribe →   │    bars — types where the
 │                      type (no ⏎)    │    cursor is, never submits)
 └─────────────────────────────────────┘
```

Speech-to-text is [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
running on CPU (int8, the `base` model) — about 1–2 seconds for a short clip,
fully offline. Keystroke injection is [`ydotool`](https://github.com/ReimuNotMoe/ydotool),
which works under both Wayland and X11.

## Two modes

| Hotkey | Script | Where it types | Auto-Enter? | Use for |
|--------|--------|----------------|-------------|---------|
| **Super+Z** | `voice.sh` | focused window | **yes** | dictating prompts to Claude Code / any terminal REPL |
| **Super+A** | `voice-anywhere.sh` | wherever the cursor is | no | chat boxes, search bars, docs, emails — anything you don't want auto-submitted |

The two keep **separate recorder state** (their own wav/pid/log), so pressing one
can never collide with an in-flight capture from the other.

## Quickstart

Full, portable, copy-pasteable runbook (Fedora + Ubuntu, with the Ubuntu 22.04
`ydotool` caveat): **[`docs/INSTALL.md`](docs/INSTALL.md)**.

The short version, once the system packages and the `~/voice/.venv` are in place:

```bash
# 1. one-time root daemon so ydotool can inject keystrokes via /dev/uinput
sudo bash ~/voice/setup-ydotoold.sh

# 2. bind the hotkeys (GNOME) — see docs/INSTALL.md Step 5
#    Super+Z → ~/voice/voice.sh
#    Super+A → ~/voice/voice-anywhere.sh
```

Everything except the `ydotoold` daemon install is user-level. First transcription
downloads the whisper model once (~140 MB, cached under `~/.cache/huggingface`);
after that it's ~1–2 s.

## Safety

`ydotool` injects keystrokes at the kernel level — a runaway transcript could
spray text (and Enters) into whatever's focused, including the lock screen. Both
scripts defend against that:

- **Stray-recorder guard** — kills any orphaned recorder before starting, so a
  hotkey press landing mid-transcribe can't leave the mic held (which makes every
  later capture silent and whisper hallucinate).
- **Sanitize + hard cap** — the transcript is stripped of control characters and
  newlines and capped at `VOICE_MAX_CHARS` (default 600). A hallucinated blob is
  refused, not typed.

These were added after a real desktop-takeover incident; don't remove them.

## Configuration

Override per-run via env, or edit the defaults at the top of each script.

| Variable | Default | Effect |
|----------|---------|--------|
| `VOICE_MODEL` | `base` | whisper model — `small.en` is more accurate (~480 MB, slower on CPU) |
| `VOICE_AUTO_ENTER` | `1` (Z) / `0` (A) | `1` submits with Enter; `0` types and waits |
| `VOICE_MAX_CHARS` | `600` | refuse transcripts longer than this (misfire guard) |
| `YDOTOOL_SOCKET` | `/run/ydotoold.sock` | where the ydotoold daemon listens |

Change a hotkey in GNOME Settings → Keyboard → View and Customize Shortcuts →
Custom Shortcuts.

## Repo layout

```
voice/
├─ voice.sh             # Super+Z toggle — focused window, auto-Enter
├─ voice-anywhere.sh    # Super+A toggle — any field, no Enter
├─ transcribe.py        # faster-whisper, CPU, base model, English
├─ setup-ydotoold.sh    # one-time root daemon install
├─ ydotoold.service     # the systemd unit it installs
├─ docs/
│  └─ INSTALL.md        # full portable setup runbook
├─ LICENSE
└─ README.md
```

## Troubleshooting

- **Nothing types** → is the daemon up? `systemctl status ydotoold`, and confirm
  `/run/ydotoold.sock` exists and is owned by your uid.
- **Silent / wrong mic** → `pactl info | grep "Default Source"`, then
  `pactl set-default-source <name>` (list with `pactl list short sources`).
- **Errors** → each mode logs to `~/voice/voice.log` / `~/voice/voice-anywhere.log`.
- **Hotkey doesn't fire** → confirm GNOME (`echo $XDG_CURRENT_DESKTOP`); on other
  desktops bind the scripts to a key in that DE instead.

More cases (incl. the Ubuntu 22.04 ydotool 0.x fallback) are in `docs/INSTALL.md`.

## License

MIT — see [`LICENSE`](LICENSE).
