#!/usr/bin/env bash
# lib-mic.sh — shared mic helpers for the AI Pilled Voice scripts (voice.sh, voice-anywhere.sh).
#
# Why this exists: pw-record with no --target records from @DEFAULT_AUDIO_SOURCE@.
# Whenever a Bluetooth audio device (e.g. "Shark E1" earbuds) connects, PipeWire
# promotes it to the default source, and its capture yields pure SILENCE in this
# setup — so dictation silently records nothing. (Verified 2026-06-21: default
# bluez source rms=0/max=0; built-in alsa_input rms~7859.) These helpers (1) pick
# a real, non-Bluetooth capture device, and (2) detect a silent capture so the
# scripts can FAIL LOUDLY instead of typing nothing.
#
# Source this file; it defines choose_mic and wav_rms. No side effects on source.

# choose_mic — selects a recording source. Sets globals:
#   MIC_TARGET  -> value to pass to `pw-record --target`
#   MIC_LABEL   -> human-friendly name for notifications
# Returns non-zero only if no usable capture device exists at all.
#
# Order: explicit VOICE_SOURCE override -> the default source IF it is not bluez
# -> first non-bluez Audio/Source, preferring a built-in ALSA analog mic.
choose_mic() {
  MIC_TARGET="${VOICE_SOURCE:-}"; MIC_LABEL="$MIC_TARGET"
  [[ -n "$MIC_TARGET" ]] && return 0

  local def_name
  def_name="$(_mic_node_name @DEFAULT_AUDIO_SOURCE@)"
  if [[ -n "$def_name" && "$def_name" != *bluez* ]]; then
    MIC_TARGET="$def_name"
    MIC_LABEL="$(_mic_node_desc @DEFAULT_AUDIO_SOURCE@)"; MIC_LABEL="${MIC_LABEL:-$def_name}"
    return 0
  fi

  # Default source is Bluetooth (or missing) — walk the Sources list and take the
  # first real (non-bluez) capture device, preferring a built-in ALSA analog mic.
  local ids id name desc first_name="" first_desc=""
  ids="$(wpctl status 2>/dev/null \
        | awk '/Sources:/{f=1;next} /Filters:|Sinks:|Streams:|Video/{f=0} f' \
        | sed -nE 's/^[^0-9]*([0-9]+)\. .*/\1/p')"
  for id in $ids; do
    name="$(_mic_node_name "$id")"
    [[ -z "$name" || "$name" == *bluez* ]] && continue
    desc="$(_mic_node_desc "$id")"
    [[ -z "$first_name" ]] && { first_name="$name"; first_desc="$desc"; }
    if [[ "$name" == alsa_input* ]]; then
      MIC_TARGET="$name"; MIC_LABEL="${desc:-$name}"; return 0
    fi
  done
  MIC_TARGET="$first_name"; MIC_LABEL="${first_desc:-$first_name}"
  [[ -n "$MIC_TARGET" ]]
}

_mic_node_name() { wpctl inspect "$1" 2>/dev/null | sed -nE 's/.*node\.name = "(.*)"/\1/p' | head -1; }
_mic_node_desc() { wpctl inspect "$1" 2>/dev/null | sed -nE 's/.*node\.description = "(.*)"/\1/p' | head -1; }

# wav_rms <wavfile> — prints the integer RMS amplitude of a 16-bit mono wav, or -1
# on error. Pure stdlib (no deprecated audioop) so it survives Python 3.13+.
# Silence is ~0-5; real speech is hundreds-to-thousands on a 0..32767 scale.
wav_rms() {
  python3 - "$1" <<'PY' 2>/dev/null
import wave, sys, array, math
try:
    w = wave.open(sys.argv[1], "rb")
    a = array.array("h"); a.frombytes(w.readframes(w.getnframes()))
    print(int(math.sqrt(sum(x * x for x in a) / len(a))) if a else 0)
except Exception:
    print(-1)
PY
}
