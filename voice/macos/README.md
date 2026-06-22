# AI Pilled Voice — macOS

Push-to-talk voice dictation, ported from the Linux `~/voice` Super+Z tool.
**Tap the Right-Command key** → speak → tap again → faster-whisper transcribes
locally and types the text into the focused window, then presses Return.

Fully local / offline. No API keys.

## Pieces
- **Karabiner-Elements** — maps a *tap* of Right-⌘ to `F18` (held-with-other-keys
  still acts as Command). Rule: `~/.config/karabiner/assets/complex_modifications/voice-dictation.json`.
- **Hammerspoon** (`~/.hammerspoon/init.lua`) — binds `F18`: toggles recording,
  runs transcription, types the result + Return. Replaces Linux's gsettings + ydotool.
- **sox** — records the mic (mono 16 kHz) to `~/voice/run/voice-rec.wav`.
- **faster-whisper** (`transcribe.py`, in `~/voice/.venv`) — local STT, `base` model,
  int8/CPU, English. Identical to the Linux version.
- **voice-mac.sh** — `start`/`stop` glue (record + transcribe + safety sanitize).

## Permissions (one-time, in System Settings → Privacy & Security)
1. **Karabiner** — approve its driver extension + Input Monitoring (may need a reboot).
2. **Hammerspoon** — Accessibility (to type) and Input Monitoring if prompted.
3. **Microphone** — allow for Hammerspoon the first time you record.

## Config (env overrides honored by voice-mac.sh)
- `VOICE_MODEL` (default `base`) — try `small.en` for more accuracy.
- `VOICE_MAX_CHARS` (default `600`) — hard cap; longer transcripts are refused.
- `VOICE_SOX` — path to sox if not at `/opt/homebrew/bin/sox`.

## Test the STT half without a mic
```
say -o ~/voice/run/sample.wav --data-format=LEI16@16000 --channels=1 "hello claude this is a dictation test"
~/voice/.venv/bin/python ~/voice/transcribe.py ~/voice/run/sample.wav base
```

## Troubleshooting
- Nothing types: check `~/voice/voice.log`, confirm Hammerspoon has Accessibility.
- "Mic error": grant Microphone to Hammerspoon; test `sox -d -r16000 -c1 /tmp/t.wav` then Ctrl-C.
- Right-⌘ stopped acting as Command: disable the Karabiner rule to compare.
- Junk/foreign-language output = silent capture; a stray `sox` may hold the mic
  (`pkill -f 'sox .*voice-rec.wav'`). The script kills strays on every start/stop.
