#!/usr/bin/env python3
"""AI Pilled Voice — transcribe a wav to a single line of text (faster-whisper, CPU, local)."""
import sys
from faster_whisper import WhisperModel

wav = sys.argv[1]
name = sys.argv[2] if len(sys.argv) > 2 else "base"

# int8 on CPU: ~1-2s for short clips with the base model, fully offline.
model = WhisperModel(name, device="cpu", compute_type="int8")
segments, _info = model.transcribe(wav, language="en", vad_filter=True, beam_size=1)

text = " ".join(seg.text.strip() for seg in segments)
text = " ".join(text.split())  # collapse newlines/runs of whitespace to single spaces
sys.stdout.write(text)
