#!/usr/bin/env bash
# Meeting Recorder external watchdog. The supervisor has its own internal
# watchdog (recorder alive, audio growing, transcriber not stuck); this one
# covers the case where the supervisor process itself dies. Exits quietly when
# the supervisor writes the clean-stop marker.
set -u

RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
PIDFILE="$RUNTIME/meeting-rec.pid"
FFPIDFILE="$RUNTIME/meeting-rec.ffmpeg.pid"
INHPIDFILE="$RUNTIME/meeting-rec.inhibit.pid"
STOPPED="$RUNTIME/meeting-rec.stopped"
SND=/usr/share/sounds/freedesktop/stereo

PID="$(cat "$PIDFILE" 2>/dev/null)" || exit 0
[[ -n "$PID" ]] || exit 0

while sleep 2; do
  [[ -f "$STOPPED" ]] && exit 0
  if ! kill -0 "$PID" 2>/dev/null; then
    sleep 1                       # grace for a clean exit racing the check
    [[ -f "$STOPPED" ]] && exit 0
    # reap the orphaned recorder so it doesn't record forever unsupervised
    FFPID="$(cat "$FFPIDFILE" 2>/dev/null)"
    [[ -n "${FFPID:-}" ]] && kill -9 "$FFPID" 2>/dev/null
    rm -f "$FFPIDFILE"
    # release the suspend lock the dead supervisor was holding
    INHPID="$(cat "$INHPIDFILE" 2>/dev/null)"
    [[ -n "${INHPID:-}" ]] && kill "$INHPID" 2>/dev/null
    rm -f "$INHPIDFILE"
    MSG=$'Meeting recorder process DIED unexpectedly.\nRecording and transcription have STOPPED.\nPartial files are in ~/Meetings/latest'
    notify-send -a "Meeting Recorder" -u critical "⚠ MEETING RECORDING STOPPED" "$MSG" || true
    ( for _ in 1 2 3 4 5 6; do paplay "$SND/alarm-clock-elapsed.oga" 2>/dev/null; done ) &
    zenity --error --title="MEETING RECORDING STOPPED" --text="$MSG" 2>/dev/null
    rm -f "$PIDFILE"
    exit 1
  fi
done
