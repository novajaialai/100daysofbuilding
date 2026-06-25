#!/usr/bin/env python3
"""
Voice daemon — keeps faster-whisper warm between dictation presses.
Listens on a Unix socket; receives a wav path, returns the transcript.

Protocol (newline-delimited UTF-8):
  client → "/path/to/audio.wav\n"
  daemon → "TRANSCRIPT: the text here\n"
         | "TRANSCRIPT: \n"          (silence / nothing heard)
         | "ERROR: reason\n"

One connection at a time — transcription is inherently sequential.
Falls back gracefully: if the daemon isn't running, voice.sh calls
transcribe.py directly (cold-load, ~1-2s slower but always works).
"""
import os, sys, socket, logging, signal, time
from pathlib import Path

RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCK_PATH = Path(RUNTIME) / "voice-daemon.sock"
LOG_PATH  = Path.home() / "voice" / "voice-daemon.log"
VENV_DIR  = Path.home() / "voice" / ".venv"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("voice-daemon")


def load_model():
    model_name = os.environ.get("VOICE_MODEL", "base")
    log.info("loading faster-whisper model '%s' (cpu, int8)…", model_name)
    t0 = time.monotonic()
    from faster_whisper import WhisperModel
    m = WhisperModel(model_name, device="cpu", compute_type="int8")
    log.info("model ready in %.1fs", time.monotonic() - t0)
    return m, model_name


def transcribe(model, wav_path: str) -> str:
    from faster_whisper import WhisperModel
    t0 = time.monotonic()
    segments, _ = model.transcribe(
        wav_path, language="en", vad_filter=True, beam_size=1
    )
    text = " ".join(seg.text.strip() for seg in segments)
    text = " ".join(text.split())
    log.info("transcribed '%s' in %.1fs → %r", wav_path, time.monotonic() - t0, text[:80])
    return text


def handle(conn, model):
    try:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        wav_path = data.decode().strip()
        if not wav_path:
            conn.sendall(b"ERROR: empty request\n")
            return
        if not os.path.exists(wav_path):
            conn.sendall(f"ERROR: file not found: {wav_path}\n".encode())
            return
        text = transcribe(model, wav_path)
        conn.sendall(f"TRANSCRIPT: {text}\n".encode())
    except Exception as e:
        log.exception("handle error")
        try:
            conn.sendall(f"ERROR: {e}\n".encode())
        except Exception:
            pass
    finally:
        conn.close()


def main():
    log.info("voice-daemon starting (pid %d)", os.getpid())

    # Remove stale socket
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()

    model, model_name = load_model()

    # Clean shutdown on SIGTERM/SIGINT
    def _shutdown(sig, frame):
        log.info("shutting down (signal %d)", sig)
        try: SOCK_PATH.unlink()
        except Exception: pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK_PATH))
    SOCK_PATH.chmod(0o600)
    srv.listen(4)
    log.info("listening on %s (model=%s)", SOCK_PATH, model_name)

    while True:
        try:
            conn, _ = srv.accept()
            handle(conn, model)
        except OSError as e:
            if e.errno == 9:  # bad file descriptor after shutdown
                break
            log.error("accept error: %s", e)


if __name__ == "__main__":
    main()
