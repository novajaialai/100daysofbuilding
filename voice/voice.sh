#!/usr/bin/env bash
# AI Pilled Voice — push-to-talk dictation into the focused window.
# Toggle: first press starts recording; second press transcribes + types it + Enter.
# Bound to Super+Z via a GNOME custom keybinding.
set -uo pipefail

DIR="$HOME/voice"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
WAV="$RUNTIME/voice-rec.wav"
PIDFILE="$RUNTIME/voice-rec.pid"
MICFILE="$RUNTIME/voice-rec.mic"
CANCELFILE="$RUNTIME/voice-rec.cancel"  # press key during transcription to cancel
LOG="$DIR/voice.log"
VENV="$DIR/.venv"
NOTIFY_ID_FILE="$RUNTIME/voice-rec.notify-id"

source "$DIR/lib-mic.sh"

# --- config: load file first, then env overrides take precedence -----------
[[ -f "$HOME/.config/voice/config" ]] && source "$HOME/.config/voice/config"
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-/run/ydotoold.sock}"
MODEL="${VOICE_MODEL:-base}"
AUTO_ENTER="${VOICE_AUTO_ENTER:-1}"
MIN_RMS="${VOICE_MIN_RMS:-30}"
# ---------------------------------------------------------------------------

PYTHON="${VENV}/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="python3"  # fallback to system python

SND=/usr/share/sounds/freedesktop/stereo
beep_start(){ paplay "$SND/dialog-information.oga" >/dev/null 2>&1 & }
beep_stop(){  paplay "$SND/message.oga"            >/dev/null 2>&1 & }
beep_done(){  paplay "$SND/complete.oga"           >/dev/null 2>&1 & }

# Persistent notification: replaces itself in-place while recording.
# Uses notify-send --replace-id so one toast stays visible the entire session.
NOTE_ID_FILE="$RUNTIME/voice-rec.nid"
note_persist(){
  local title="$1" body="${2:-}"
  local id; id=$(cat "$NOTE_ID_FILE" 2>/dev/null || echo "0")
  local new_id
  new_id=$(notify-send --print-id --replace-id "$id" -t 0 -a "Voice" "$title" "$body" 2>/dev/null || echo "0")
  echo "$new_id" > "$NOTE_ID_FILE"
}
note_dismiss(){
  local id; id=$(cat "$NOTE_ID_FILE" 2>/dev/null || echo "0")
  [[ "$id" != "0" ]] && notify-send --replace-id "$id" -t 1500 -a "Voice" "$1" "${2:-}" >/dev/null 2>&1 || true
  rm -f "$NOTE_ID_FILE"
}
note(){ notify-send -t 2500 -a "Voice" "$1" "${2:-}" >/dev/null 2>&1 || true; }

recording(){ [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }
kill_strays(){ pkill -f "pw-record .*${WAV}" 2>/dev/null || true; }

# Try the warm daemon first; fall back to cold transcribe.py if unavailable.
DAEMON_SOCK="$RUNTIME/voice-daemon.sock"
transcribe_wav() {
  local wav="$1" model="$2"
  if [[ -S "$DAEMON_SOCK" ]]; then
    local resp
    resp="$("$PYTHON" "$DIR/daemon_client.py" "$wav" "$DAEMON_SOCK" 2>>"$LOG")"
    if [[ "$resp" == TRANSCRIPT:* ]]; then
      echo "${resp#TRANSCRIPT: }"
      return 0
    fi
    echo "voice.sh: daemon gave no usable reply (got: ${resp:0:80}), falling back to cold-load" >>"$LOG"
  fi
  # Fallback: cold-load (always works, ~1-2s slower)
  "$PYTHON" "$DIR/transcribe.py" "$wav" "$model" 2>>"$LOG"
}

if recording; then
  # ---- STOP: finalize recording, transcribe, inject ----
  PID="$(cat "$PIDFILE")"; rm -f "$PIDFILE"
  kill "$PID" 2>/dev/null
  for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.05; done
  kill_strays
  beep_stop

  MIC_LABEL="$(cat "$MICFILE" 2>/dev/null || echo 'mic')"; rm -f "$MICFILE"
  RMS="$(wav_rms "$WAV")"
  if (( RMS < MIN_RMS )); then
    note_dismiss "No audio captured" "Mic '$MIC_LABEL' was silent (rms=$RMS). BT mic hijacking? Run: voice-health"
    exit 0
  fi

  note_persist "⏳ Transcribing…" "Press Super+Z to cancel"

  TEXT="$(transcribe_wav "$WAV" "$MODEL")"
  TEXT="${TEXT#"${TEXT%%[![:space:]]*}"}"
  TEXT="${TEXT%"${TEXT##*[![:space:]]}"}"

  # Cancel gesture: if Super+Z was pressed during transcription, bail out
  if [[ -f "$CANCELFILE" ]]; then
    rm -f "$CANCELFILE"
    note_dismiss "Cancelled" "Nothing typed."
    exit 0
  fi

  if [[ -z "$TEXT" ]]; then note_dismiss "Heard nothing" "No speech detected."; exit 0; fi

  TEXT="$(printf '%s' "$TEXT" | tr -d '\r\n' | tr -cd '[:print:]')"
  MAX="${VOICE_MAX_CHARS:-600}"
  if (( ${#TEXT} > MAX )); then
    note_dismiss "Voice: refused" "Transcript ${#TEXT} chars (> $MAX) — likely hallucination. Nothing typed."
    exit 0
  fi

  if ! ydotool type "$TEXT" 2>>"$LOG"; then
    note_dismiss "ydotool failed" "Run: voice-health"; exit 1
  fi
  if [[ "$AUTO_ENTER" == "1" ]]; then sleep 0.12; ydotool key 28:1 28:0 2>>"$LOG"; fi
  beep_done
  note_dismiss "Sent ✓" "${TEXT:0:60}$(( ${#TEXT} > 60 ? 1 : 0 ))&&echo '…' || true"

elif [[ -f "$CANCELFILE" ]]; then
  # Already cancelled — clear the flag silently
  rm -f "$CANCELFILE"

else
  # ---- START: detach a recorder that survives this script exiting ----
  kill_strays
  rm -f "$WAV" "$PIDFILE" "$MICFILE" "$CANCELFILE"
  if ! choose_mic; then
    note "Mic error" "No working capture device found. Run: voice-health"; exit 1
  fi
  printf '%s' "$MIC_LABEL" > "$MICFILE"
  setsid pw-record --target "$MIC_TARGET" --rate 16000 --channels 1 --format s16 "$WAV" >/dev/null 2>/dev/null &
  REC=$!
  sleep 0.15
  if ! kill -0 "$REC" 2>/dev/null; then
    note "Mic error" "Recorder failed to start. Run: voice-health"; exit 1
  fi
  echo "$REC" > "$PIDFILE"
  beep_start
  note_persist "● Recording… ($MIC_LABEL)" "Super+Z again to send • or to cancel during transcription"
fi
