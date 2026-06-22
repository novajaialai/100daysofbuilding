#!/usr/bin/env bash
# AI Pilled Voice (macOS) — push-to-talk dictation, ported from the Linux Super+Z tool.
#   voice-mac.sh start  -> begin recording (detached), returns immediately
#   voice-mac.sh stop   -> stop recording, transcribe, print sanitized text to stdout
# Hammerspoon owns the hotkey, the toggle state, and the keystroke injection;
# this script owns recording (sox) + transcription (faster-whisper), mirroring voice.sh.
set -uo pipefail

DIR="$HOME/voice"
RUN="$DIR/run"
WAV="$RUN/voice-rec.wav"
PIDFILE="$RUN/voice-rec.pid"
LOG="$DIR/voice.log"
mkdir -p "$RUN"

MODEL="${VOICE_MODEL:-base}"               # base | small.en | medium ...
PY="$DIR/.venv/bin/python"
SOX="${VOICE_SOX:-/opt/homebrew/bin/sox}"; [[ -x "$SOX" ]] || SOX="$(command -v sox || echo sox)"

# Kill ANY recorder writing our wav — guards against an orphaned sox holding the
# mic (a tap landing mid-transcribe), which would make later captures silent and
# make whisper hallucinate. Mirrors the Linux kill_strays guard.
kill_strays(){ pkill -f "sox .*${WAV}" 2>/dev/null || true; }

case "${1:-}" in
  start)
    kill_strays
    rm -f "$WAV" "$PIDFILE"
    # record mono 16k signed-16 from the default input until interrupted
    nohup "$SOX" -q -d -r 16000 -c 1 -b 16 -e signed-integer "$WAV" >/dev/null 2>>"$LOG" &
    REC=$!
    sleep 0.2
    if ! kill -0 "$REC" 2>/dev/null; then echo "__MIC_ERROR__"; exit 1; fi
    echo "$REC" > "$PIDFILE"
    echo "__LISTENING__"
    ;;
  stop)
    if [[ -f "$PIDFILE" ]]; then
      PID="$(cat "$PIDFILE")"; rm -f "$PIDFILE"
      kill -INT "$PID" 2>/dev/null          # SIGINT so sox finalizes the WAV header cleanly
      for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.05; done
    fi
    kill_strays
    [[ -f "$WAV" ]] || { echo ""; exit 0; }

    TEXT="$("$PY" "$DIR/transcribe.py" "$WAV" "$MODEL" 2>>"$LOG")"
    # trim leading/trailing whitespace
    TEXT="${TEXT#"${TEXT%%[![:space:]]*}"}"
    TEXT="${TEXT%"${TEXT##*[![:space:]]}"}"
    # safety: strip control chars/newlines + hard-cap length, so a hallucination
    # on a silent capture can't spray a runaway blob (with Enters) into the window.
    TEXT="$(printf '%s' "$TEXT" | tr -d '\r\n' | LC_ALL=C tr -cd '[:print:]')"
    MAX="${VOICE_MAX_CHARS:-600}"
    if (( ${#TEXT} > MAX )); then echo "__TOO_LONG__"; exit 0; fi
    printf '%s' "$TEXT"
    ;;
  *)
    echo "usage: $0 {start|stop}" >&2; exit 2 ;;
esac
