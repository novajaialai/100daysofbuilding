#!/usr/bin/env bash
# Call & Meeting Recorder — toggle. First press starts recording + live
# transcription; second press stops, finalizes the transcript, saves to
# ~/Meetings/. Bound to Super+R via a GNOME custom keybinding.
#
# Modes:
#   meeting.sh            toggle recording (default; what Super+R runs)
#   meeting.sh sources    list audio sources (find a phone's far-end source)
#   meeting.sh phone      record with a bridged phone (Bluetooth HFP or line-in)
#                         as the far end instead of the laptop's system audio
set -uo pipefail

DIR="$HOME/meeting"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
PIDFILE="$RUNTIME/meeting-rec.pid"
STOPPED="$RUNTIME/meeting-rec.stopped"
LOG="$DIR/meeting.log"
SND=/usr/share/sounds/freedesktop/stereo

alarm() {
  notify-send -a "Meeting Recorder" -u critical "⚠ MEETING RECORDER FAILED" "$1" || true
  ( for _ in 1 2 3 4 5; do paplay "$SND/alarm-clock-elapsed.oga" 2>/dev/null; done ) &
  zenity --error --title="MEETING RECORDER FAILED" --text="$1" 2>/dev/null &
}

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

# Annotate each PulseAudio/PipeWire source with what it is, so the phone's
# far-end source is easy to spot.
hint_for() {
  case "$1" in
    bluez*.monitor)  echo "monitor of a Bluetooth output";;
    *.monitor)       echo "system audio (far end of meetings & VoIP — the default)";;
    bluez_input.*|bluez_source.*) echo "📞 Bluetooth — a paired phone/headset (far end of a cell call over HFP)";;
    *usb*input*|alsa_input.usb*) echo "USB / line-in input (dongle — can carry a phone)";;
    *analog*input*|alsa_input.pci*) echo "built-in microphone (your voice)";;
    *)               echo "audio input";;
  esac
}

list_sources() {
  echo "Audio sources on this machine (NAME — what it is):"
  echo "  Set the far end with:  MEETING_FAR_SOURCE=<name> ~/meeting/meeting.sh"
  echo
  local def; def="$(pactl get-default-source 2>/dev/null)"
  pactl list short sources 2>/dev/null | while IFS=$'\t' read -r _id name _rest; do
    [[ -n "$name" ]] || continue
    local mark=" "; [[ "$name" == "$def" ]] && mark="*"
    printf "  %s %-55s %s\n" "$mark" "$name" "$(hint_for "$name")"
  done
  echo
  echo "  (* = current default source)"
}

# Best guess at a bridged phone's far-end source: a Bluetooth headset source
# first, else a USB/line-in input that isn't the built-in mic.
detect_phone_source() {
  local names; names="$(pactl list short sources 2>/dev/null | cut -f2)"
  local bt; bt="$(printf '%s\n' "$names" | grep -vE '\.monitor$' \
                  | grep -m1 -E '^bluez_(input|source)\.')" || true
  if [[ -n "$bt" ]]; then echo "$bt"; return 0; fi
  printf '%s\n' "$names" \
    | grep -v '\.monitor$' | grep -viE 'analog.*input|alsa_input\.pci' \
    | grep -iE 'usb|line' | head -1
}

MODE="${1:-toggle}"

# `sources`: just print and exit — never touches recording.
if [[ "$MODE" == "sources" ]]; then
  list_sources
  exit 0
fi

if running; then
  # ---- STOP: supervisor finalizes remaining chunks, notifies, exits ----
  kill -USR1 "$(cat "$PIDFILE")"
  exit 0
fi

# A leftover pidfile with a dead pid means a previous crash (the watchdog
# should already have alarmed). Clear state and start fresh.
rm -f "$PIDFILE" "$STOPPED" "$RUNTIME/meeting-rec.ffmpeg.pid"

# `phone`: capture a bridged phone as the far end. Pick its source (Bluetooth
# HFP, else line-in/USB) and hand it to the supervisor via MEETING_FAR_SOURCE.
if [[ "$MODE" == "phone" ]]; then
  far="${MEETING_FAR_SOURCE:-$(detect_phone_source)}"
  if [[ -z "$far" ]]; then
    alarm $'No phone audio source found.\nPair the phone over Bluetooth (as a headset/HFP) or plug in a line-in/USB device, then:\n  ~/meeting/meeting.sh sources'
    exit 1
  fi
  export MEETING_FAR_SOURCE="$far"
  notify-send -a "Meeting Recorder" "Recording call — far end: ${far}" \
    "Your mic = You · ${far} = Them" 2>/dev/null || true
fi

# The supervisor takes its own systemd-inhibit lock (sleep/idle/lid) for exactly
# the duration of active recording, so the machine can't suspend mid-call but is
# free to sleep again the moment recording stops — even while the saved-transcript
# window is left open for reading.
setsid python3 "$DIR/supervisor.py" >>"$LOG" 2>&1 &
SUP=$!

# Verify startup: the supervisor only writes its pidfile after the recorder
# process and window are actually up. If that doesn't happen, scream.
ok=0
for _ in $(seq 1 40); do
  sleep 0.5
  [[ -f "$PIDFILE" ]] && { ok=1; break; }
  kill -0 "$SUP" 2>/dev/null || break   # supervisor died during startup
done
if [[ "$ok" != 1 ]] || ! running; then
  alarm $'Recorder did NOT start. Nothing is being recorded.\nSee '"$LOG"
  exit 1
fi

# External safety net: alarms if the supervisor process itself dies.
setsid bash "$DIR/watchdog.sh" >>"$LOG" 2>&1 &
exit 0
