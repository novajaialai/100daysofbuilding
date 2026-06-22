#!/usr/bin/env bash
# AI Pilled Voice — macOS setup (Apple Silicon). Idempotent.
# Ports the Linux Super+Z dictation tool to macOS: TAP Right-Command -> speak ->
# tap again -> faster-whisper transcribes locally -> text types in + Return.
# After this runs you still grant Microphone + Accessibility + Karabiner driver
# permissions in System Settings (macOS requires a human for those).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo ">>> Enter your Mac password once (Homebrew + app casks need admin):"
sudo -v
( while true; do sudo -n true 2>/dev/null; sleep 30; kill -0 "$$" 2>/dev/null || exit; done ) &
KEEP=$!; trap 'kill $KEEP 2>/dev/null' EXIT

# 1. Homebrew
if [ ! -x /opt/homebrew/bin/brew ] && ! command -v brew >/dev/null 2>&1; then
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
eval "$(/opt/homebrew/bin/brew shellenv)"
grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"

# 2. Dependencies
brew install --cask hammerspoon karabiner-elements
brew install sox python@3.12

# 3. Place files
mkdir -p "$HOME/voice/run" "$HOME/.hammerspoon" "$HOME/.config/karabiner/assets/complex_modifications"
cp "$HERE/transcribe.py" "$HERE/voice-mac.sh" "$HERE/README.md" "$HOME/voice/"
chmod +x "$HOME/voice/voice-mac.sh"
cp "$HERE/hammerspoon-init.lua" "$HOME/.hammerspoon/init.lua"
cp "$HERE/karabiner-voice-dictation.json" "$HOME/.config/karabiner/assets/complex_modifications/voice-dictation.json"

# 4. Python venv + faster-whisper
/opt/homebrew/bin/python3.12 -m venv "$HOME/voice/.venv"
"$HOME/voice/.venv/bin/pip" install -q -U pip faster-whisper

# 5. Pre-download the base model (warm cache) using macOS `say`
say -o "$HOME/voice/run/_warm.wav" --data-format=LEI16@16000 --channels=1 "warming up the model" || true
"$HOME/voice/.venv/bin/python" "$HOME/voice/transcribe.py" "$HOME/voice/run/_warm.wav" base >/dev/null 2>&1 || true
rm -f "$HOME/voice/run/_warm.wav"

# 6. Launch apps + inject the Karabiner rule (Karabiner must create its config once)
open -a Karabiner-Elements 2>/dev/null || true
open -a Hammerspoon 2>/dev/null || true
sleep 3
if [ -f "$HOME/.config/karabiner/karabiner.json" ]; then
  /usr/bin/python3 "$HERE/inject_karabiner.py"
else
  echo "NOTE: open Karabiner once, then run:  python3 $HERE/inject_karabiner.py"
fi

cat <<'EOF'

============================================================
 Almost done — grant these in System Settings (one-time):
   • Karabiner   : driver extension + Input Monitoring
   • Hammerspoon : Accessibility
   • Microphone  : allow Hammerspoon on first dictation
 Then TAP Right-Command, speak, tap again -> text types in + Return.
============================================================
EOF
