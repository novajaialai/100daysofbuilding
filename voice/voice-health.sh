#!/usr/bin/env bash
# voice-health — checks every component in the dictation pipeline and reports pass/fail.
# Run this any time dictation stops working to find out exactly what's broken.
# Exit 0 = all green. Exit 1 = something is wrong (details printed).
set -uo pipefail

DIR="$HOME/voice"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
YDOTOOL_SOCK="${YDOTOOL_SOCKET:-/run/ydotoold.sock}"
DAEMON_SOCK="$RUNTIME/voice-daemon.sock"
VENV="$DIR/.venv"
PASS=0; FAIL=0

ok()   { echo "  [✓] $*"; ((PASS++)) || true; }
fail() { echo "  [✗] $*"; ((FAIL++)) || true; }
info() { echo "  [·] $*"; }

echo ""
echo "Voice Health Check — $(date '+%Y-%m-%d %H:%M')"
echo "────────────────────────────────────────────────"

# 1. ydotoold
echo ""
echo "  Input injection (ydotoold)"
if systemctl is-active --quiet ydotoold 2>/dev/null; then
  ok "ydotoold service running"
else
  fail "ydotoold not running — fix: sudo systemctl start ydotoold"
fi
if [[ -S "$YDOTOOL_SOCK" ]]; then
  ok "socket $YDOTOOL_SOCK exists"
  if YDOTOOL_SOCKET="$YDOTOOL_SOCK" ydotool type "" 2>/dev/null; then
    ok "ydotool type responds"
  else
    fail "ydotool type failed — socket exists but ydotoold may be hung; try: sudo systemctl restart ydotoold"
  fi
else
  fail "socket $YDOTOOL_SOCK missing — fix: sudo systemctl start ydotoold"
fi

# 2. PipeWire / mic
echo ""
echo "  Audio capture (PipeWire)"
if command -v pw-record &>/dev/null; then
  ok "pw-record available"
else
  fail "pw-record not found — install pipewire-utils"
fi

source "$DIR/lib-mic.sh" 2>/dev/null
if choose_mic 2>/dev/null; then
  if [[ "$MIC_TARGET" == *bluez* ]]; then
    fail "selected mic is Bluetooth ($MIC_TARGET) — this usually gives silence. Set VOICE_SOURCE to a built-in mic or disconnect BT device."
  else
    ok "mic selected: $MIC_LABEL ($MIC_TARGET)"
  fi
else
  fail "no usable capture device found (wpctl returned no non-BT sources)"
fi

# Source volume check
VOL=$(wpctl get-volume @DEFAULT_AUDIO_SOURCE@ 2>/dev/null | awk '{print $2}' || echo "0")
VOL_INT=$(echo "$VOL" | awk '{printf "%d", $1 * 100}')
if (( VOL_INT < 30 )); then
  fail "source volume very low (${VOL_INT}%) — fix: wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 1.0"
elif (( VOL_INT < 70 )); then
  info "source volume is ${VOL_INT}% (consider raising: wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 1.0)"
else
  ok "source volume ${VOL_INT}%"
fi

# Check for orphaned pw-record
WAV_Z="$RUNTIME/voice-rec.wav"; WAV_A="$RUNTIME/voice-anywhere-rec.wav"
if pgrep -f "pw-record .*voice-rec\.wav" &>/dev/null; then
  fail "orphaned pw-record found holding voice-rec.wav — killing it"
  pkill -f "pw-record .*voice-rec\.wav" 2>/dev/null || true
elif pgrep -f "pw-record .*voice-anywhere" &>/dev/null; then
  fail "orphaned pw-record found holding voice-anywhere-rec.wav — killing it"
  pkill -f "pw-record .*voice-anywhere" 2>/dev/null || true
else
  ok "no orphaned pw-record processes"
fi

# 3. Transcription (faster-whisper)
echo ""
echo "  Transcription (faster-whisper)"
if [[ -d "$VENV" ]]; then
  PYTHON="$VENV/bin/python3"
  ok "venv exists ($VENV)"
else
  PYTHON="python3"
  info "no venv — using system python (run: $DIR/setup-ydotoold.sh or create venv manually)"
fi
PY_VER=$("$PYTHON" --version 2>&1)
ok "python: $PY_VER"
if "$PYTHON" -c "from faster_whisper import WhisperModel" 2>/dev/null; then
  ok "faster-whisper importable"
else
  fail "faster-whisper import failed — fix: $PYTHON -m pip install faster-whisper==1.2.1"
fi

# Model cache
CACHE="$HOME/.cache/huggingface/hub"
for MODEL_NAME in base tiny; do
  if ls "$CACHE"/models--Systran--faster-whisper-"$MODEL_NAME"/snapshots/*/model.bin &>/dev/null 2>&1; then
    ok "model cached: $MODEL_NAME"
  else
    info "model not cached: $MODEL_NAME (will download on first use)"
  fi
done

# 4. Warm daemon
echo ""
echo "  Warm daemon (voice-daemon)"
if systemctl --user is-active --quiet voice-daemon 2>/dev/null; then
  ok "voice-daemon service running"
  if [[ -S "$DAEMON_SOCK" ]]; then
    ok "daemon socket $DAEMON_SOCK exists"
    RESP=$(echo "/dev/null" | timeout 3 socat - "UNIX-CONNECT:$DAEMON_SOCK" 2>/dev/null | head -1 || echo "")
    if [[ "$RESP" == ERROR:* ]]; then
      ok "daemon responds (got expected error for /dev/null)"
    elif [[ -n "$RESP" ]]; then
      ok "daemon responds"
    else
      info "daemon socket exists but didn't respond (may be loading model)"
    fi
  else
    fail "daemon service running but socket missing — try: systemctl --user restart voice-daemon"
  fi
else
  info "voice-daemon not running (optional; scripts fall back to cold-load without it)"
fi

# 5. Quick end-to-end transcription test on silence
echo ""
echo "  End-to-end: transcribe silence"
TEST_WAV=$(mktemp /tmp/voice-health-XXXXXX.wav)
"$PYTHON" - "$TEST_WAV" <<'PY' 2>/dev/null
import wave, struct, sys
with wave.open(sys.argv[1], 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(struct.pack('<' + 'h'*8000, *([0]*8000)))
PY
RESULT=$("$PYTHON" "$DIR/transcribe.py" "$TEST_WAV" base 2>/tmp/voice-health-err.txt || echo "__FAILED__")
rm -f "$TEST_WAV"
if [[ "$RESULT" == "__FAILED__" ]]; then
  fail "transcribe.py crashed — check: cat /tmp/voice-health-err.txt"
else
  ok "transcribe.py runs (returned: '${RESULT:-<empty, expected for silence>}')"
fi
if [[ -s /tmp/voice-health-err.txt ]]; then
  info "transcription stderr: $(cat /tmp/voice-health-err.txt | head -3)"
fi

# Summary
echo ""
echo "────────────────────────────────────────────────"
if (( FAIL == 0 )); then
  echo "  All $PASS checks passed. Dictation should work."
else
  echo "  $FAIL check(s) FAILED, $PASS passed. Fix the items above."
fi
echo ""
exit $(( FAIL > 0 ? 1 : 0 ))
