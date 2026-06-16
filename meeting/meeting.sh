#!/usr/bin/env bash
# Meeting Recorder — toggle. First press starts recording + live transcription;
# second press stops, finalizes the transcript, and saves to ~/Meetings/.
# Bound to Super+R via a GNOME custom keybinding.
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

if running; then
  # ---- STOP: supervisor finalizes remaining chunks, notifies, exits ----
  kill -USR1 "$(cat "$PIDFILE")"
  exit 0
fi

# A leftover pidfile with a dead pid means a previous crash (the watchdog
# should already have alarmed). Clear state and start fresh.
rm -f "$PIDFILE" "$STOPPED" "$RUNTIME/meeting-rec.ffmpeg.pid"

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
