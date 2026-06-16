#!/usr/bin/env bash
# AI Pilled Voice (Anywhere) — push-to-talk dictation into WHATEVER field is focused.
# Toggle: first press starts recording; second press transcribes + types it where the
# cursor is — in ANY window/field. No auto-Enter, so it never submits a form/chat for you.
# Bound to Super+A via a GNOME custom keybinding. Sibling of voice.sh (Super+Z, terminal+Enter).
#
# It keeps its OWN recorder state (separate wav/pid/log) so it can never collide with an
# in-flight Super+Z capture. Shares ~/voice/transcribe.py with the Super+Z script.
set -uo pipefail

DIR="$HOME/voice"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
WAV="$RUNTIME/voice-anywhere-rec.wav"
PIDFILE="$RUNTIME/voice-anywhere-rec.pid"
LOG="$DIR/voice-anywhere.log"

# --- config (override via env) ---------------------------------------------
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-/run/ydotoold.sock}"
MODEL="${VOICE_MODEL:-base}"          # base | small | small.en | medium ...
AUTO_ENTER="${VOICE_AUTO_ENTER:-0}"   # 0 = leave text for review (default here), 1 = submit
# ---------------------------------------------------------------------------

SND=/usr/share/sounds/freedesktop/stereo
beep_start(){ paplay "$SND/dialog-information.oga" >/dev/null 2>&1 & }
beep_stop(){  paplay "$SND/message.oga"            >/dev/null 2>&1 & }
beep_done(){  paplay "$SND/complete.oga"           >/dev/null 2>&1 & }
note(){ notify-send -t 2500 -a "Voice" "$1" "${2:-}" >/dev/null 2>&1 || true; }

recording(){ [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }
# Kill ANY recorder writing OUR wav. Guards against orphaned / stacked pw-record
# processes — the #1 way this wedges is a press landing during the slow transcribe,
# which leaves a recorder holding the mic so every later capture is silent (whisper
# then hallucinates). Scoped to our wav so it never disturbs the Super+Z recorder.
kill_strays(){ pkill -f "pw-record .*${WAV}" 2>/dev/null || true; }

if recording; then
  # ---- STOP: finalize recording, transcribe, inject ----
  PID="$(cat "$PIDFILE")"; rm -f "$PIDFILE"
  kill "$PID" 2>/dev/null
  for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.05; done
  kill_strays  # belt-and-suspenders: no recorder should survive the stop
  beep_stop; note "Transcribing…"

  TEXT="$(python3 "$DIR/transcribe.py" "$WAV" "$MODEL" 2>>"$LOG")"
  # trim leading/trailing whitespace
  TEXT="${TEXT#"${TEXT%%[![:space:]]*}"}"
  TEXT="${TEXT%"${TEXT##*[![:space:]]}"}"

  if [[ -z "$TEXT" ]]; then note "Heard nothing" "No speech detected."; exit 0; fi

  # Safety (matches voice.sh, after the 2026-06-14 desktop-takeover incident): strip
  # control chars / newlines and HARD-CAP length, so a whisper hallucination on a
  # silent/strayed capture can never spray a runaway blob into the focused window
  # via kernel-level ydotool.
  TEXT="$(printf '%s' "$TEXT" | tr -d '\r\n' | tr -cd '[:print:]')"
  MAX="${VOICE_MAX_CHARS:-600}"
  if (( ${#TEXT} > MAX )); then
    note "Voice: refused" "Transcript ${#TEXT} chars (> $MAX) — likely a misfire. Nothing typed."
    exit 0
  fi

  if ! ydotool type "$TEXT" 2>>"$LOG"; then
    note "ydotool failed" "Is ydotoold running? Check voice-anywhere.log"; exit 1
  fi
  if [[ "$AUTO_ENTER" == "1" ]]; then sleep 0.12; ydotool key 28:1 28:0 2>>"$LOG"; fi
  beep_done; note "Typed ✓" "$TEXT"
else
  # ---- START: detach a recorder that survives this script exiting ----
  kill_strays            # clear any orphan holding the mic before we begin
  rm -f "$WAV" "$PIDFILE"
  setsid pw-record --rate 16000 --channels 1 --format s16 "$WAV" >/dev/null 2>/dev/null &
  REC=$!
  sleep 0.15
  if ! kill -0 "$REC" 2>/dev/null; then
    note "Mic error" "Recorder failed to start — check the microphone."; exit 1
  fi
  echo "$REC" > "$PIDFILE"
  beep_start; note "Listening… (anywhere)" "Super+A again to type it where your cursor is"
fi
