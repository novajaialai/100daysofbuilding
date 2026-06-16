#!/usr/bin/env python3
"""Meeting Recorder supervisor.

Records mic + system audio (both sides of a meeting) via ffmpeg into 20s wav
chunks, live-transcribes each finished chunk with faster-whisper, and appends
to a timestamped transcript file. Shows a GTK window with a blinking REC dot,
elapsed time, and the live transcript.

Fails LOUDLY: an internal watchdog checks every 2s that the recorder process
is alive, the audio file is actually growing, the audio isn't pure digital
silence, and the transcriber isn't stuck. Any failure -> red window, alarm
sound, critical notification. An external watchdog (watchdog.sh) alarms if
this whole process dies.
"""
import datetime
import os
import signal
import subprocess
import sys
import threading
import time
import wave

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib, Gio  # noqa: E402

CHUNK_SECS = int(os.environ.get("MEETING_CHUNK_SECS", "20"))
MODEL = os.environ.get("MEETING_MODEL", "base")
# N consecutive all-zero chunks => loud "no audio" warning. 0 disables.
SILENCE_CHUNKS = int(os.environ.get("MEETING_SILENCE_CHUNKS", "3"))
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
PIDFILE = os.path.join(RUNTIME, "meeting-rec.pid")
FFPIDFILE = os.path.join(RUNTIME, "meeting-rec.ffmpeg.pid")
STOPPED = os.path.join(RUNTIME, "meeting-rec.stopped")
SND = "/usr/share/sounds/freedesktop/stereo"
MEETINGS = os.path.expanduser("~/Meetings")

CSS = """
window { background-color: #14171c; }
window.warn { background-color: #5c3a00; }
window.failed { background-color: #6e1010; }
label { color: #e6e9ee; }
.rec-dot { color: #ff3b30; font-size: 22px; }
.rec-dot.off { color: #3a3f47; }
.rec-dot.done { color: #34c759; }
.rec-title { font-size: 17px; font-weight: bold; }
.status { color: #9aa3ad; font-size: 12px; }
window.failed .status, window.warn .status {
  color: #ffffff; font-size: 15px; font-weight: bold;
}
.path { color: #6f7882; font-size: 11px; }
textview, textview text { background-color: #0c0e11; color: #cfd6dd; }
"""


def notify(summary, body="", urgency="normal"):
    subprocess.Popen(
        ["notify-send", "-a", "Meeting Recorder", "-u", urgency, summary, body],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def play(sound, repeat=1):
    def run():
        for _ in range(repeat):
            subprocess.run(["paplay", f"{SND}/{sound}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=run, daemon=True).start()


def wav_peak(path):
    """Max abs sample of a wav file; 0 = pure digital silence, -1 = unreadable."""
    try:
        import numpy as np
        with wave.open(path) as w:
            frames = w.readframes(w.getnframes())
        if not frames:
            return 0
        a = np.frombuffer(frames, dtype=np.int16)
        return int(np.abs(a).max()) if a.size else 0
    except Exception:
        return -1


SUMMARY_PROMPT = (
    "Below is a meeting transcript with timestamps. Write a concise summary: "
    "2-4 sentences on what the meeting was about, then bullet lists for key "
    "points, decisions made, and action items (omit a list if there are none). "
    "Output plain markdown, no preamble.\n\nTRANSCRIPT:\n")


def summarize(text):
    """Summarize transcript text. Claude CLI first, local ollama as fallback.
    Returns (summary, engine) or (None, error_description)."""
    errors = []
    local_bin = os.path.expanduser("~/.local/bin")
    claude = os.path.join(local_bin, "claude")
    ollama = os.path.join(local_bin, "ollama")
    try:
        r = subprocess.run(
            [claude if os.path.exists(claude) else "claude", "-p",
             SUMMARY_PROMPT + text],
            capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), "claude"
        errors.append(f"claude: rc={r.returncode} {r.stderr.strip()[:200]}")
    except Exception as e:
        errors.append(f"claude: {e}")
    try:
        r = subprocess.run(
            [ollama if os.path.exists(ollama) else "ollama", "run",
             "llama3.2:3b"],
            input=SUMMARY_PROMPT + text,
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), "ollama/llama3.2:3b"
        errors.append(f"ollama: rc={r.returncode} {r.stderr.strip()[:200]}")
    except Exception as e:
        errors.append(f"ollama: {e}")
    return None, "; ".join(errors)


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Recorder(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.jake.meeting-recorder",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.failed = False
        self.finalizing = False
        self.done = False
        self.recorder_stopped = False
        self.silence_warn = False
        self.started_at = None
        self.ffmpeg = None
        self.chunks_done = 0
        self.silent_streak = 0
        self.transcribing_since = None
        self.model_ready = False
        # watchdog state for "is the audio file growing"
        self.w_path = None
        self.w_size = -1
        self.w_last_growth = None

    # ---------------- startup ----------------
    def do_activate(self):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session = os.path.join(MEETINGS, stamp)
        self.chunk_dir = os.path.join(self.session, "chunks")
        os.makedirs(self.chunk_dir, exist_ok=True)
        self.transcript = os.path.join(self.session, "transcript.md")
        with open(self.transcript, "w") as f:
            f.write(f"# Meeting {stamp}\n\n")
        latest = os.path.join(MEETINGS, "latest")
        try:
            if os.path.islink(latest):
                os.unlink(latest)
            os.symlink(self.session, latest)
        except OSError:
            pass

        try:
            sink = subprocess.check_output(
                ["pactl", "get-default-sink"], text=True, timeout=5).strip()
            if not sink:
                raise RuntimeError("pactl returned no default sink")
        except Exception as e:
            self.startup_fail(f"Could not find audio output to capture: {e}")
            return

        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-f", "pulse", "-i", "default",          # microphone
               "-f", "pulse", "-i", f"{sink}.monitor",  # system audio (other side)
               "-filter_complex", "amix=inputs=2:duration=longest:normalize=0",
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
               "-f", "segment", "-segment_time", str(CHUNK_SECS),
               "-reset_timestamps", "1",
               os.path.join(self.chunk_dir, "chunk_%05d.wav")]
        self.rec_log = open(os.path.join(self.session, "recorder.log"), "w")
        try:
            self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                           stdout=self.rec_log, stderr=self.rec_log)
        except Exception as e:
            self.startup_fail(f"Could not start ffmpeg recorder: {e}")
            return

        self.build_window()
        self.started_at = time.time()
        self.w_last_growth = time.time()

        with open(FFPIDFILE, "w") as f:
            f.write(str(self.ffmpeg.pid))
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
        try:
            os.unlink(STOPPED)
        except FileNotFoundError:
            pass

        threading.Thread(target=self.transcriber, daemon=True).start()
        GLib.timeout_add(600, self.blink)
        GLib.timeout_add(1000, self.tick)
        GLib.timeout_add(2000, self.watchdog)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, self.request_stop)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.request_stop)
        play("dialog-information.oga")
        notify("Recording meeting", f"Transcript: {self.transcript}")

    def startup_fail(self, reason):
        print(f"STARTUP FAILURE: {reason}", file=sys.stderr, flush=True)
        notify("⚠ MEETING RECORDER FAILED TO START", reason, urgency="critical")
        play("alarm-clock-elapsed.oga", repeat=4)
        time.sleep(4)  # let the sound get out before we die
        sys.exit(1)

    # ---------------- UI ----------------
    def build_window(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)
        except AttributeError:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.win = Gtk.ApplicationWindow(application=self, title="Meeting Recorder")
        self.win.set_default_size(480, 380)
        self.win.connect("close-request", self.on_close_request)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=10, margin_bottom=10,
                      margin_start=12, margin_end=12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("rec-dot")
        self.timer = Gtk.Label(label="REC 0:00:00")
        self.timer.add_css_class("rec-title")
        header.append(self.dot)
        header.append(self.timer)
        self.stop_btn = Gtk.Button(label="Stop & save")
        self.stop_btn.connect("clicked", self.on_button)
        self.stop_btn.set_halign(Gtk.Align.END)
        self.stop_btn.set_hexpand(True)
        header.append(self.stop_btn)
        box.append(header)

        self.status = Gtk.Label(label="Loading transcription model…", xalign=0)
        self.status.add_css_class("status")
        self.status.set_wrap(True)
        box.append(self.status)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                               wrap_mode=Gtk.WrapMode.WORD_CHAR,
                               left_margin=8, right_margin=8,
                               top_margin=6, bottom_margin=6)
        self.buf = self.tv.get_buffer()
        self.end_mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
        scroll.set_child(self.tv)
        box.append(scroll)

        path_lbl = Gtk.Label(label=self.transcript, xalign=0, selectable=True)
        path_lbl.add_css_class("path")
        path_lbl.set_ellipsize(3)  # Pango.EllipsizeMode.END
        box.append(path_lbl)

        self.win.set_child(box)
        self.win.present()

    def append_ui(self, line):
        self.buf.insert(self.buf.get_end_iter(), line)
        self.buf.move_mark(self.end_mark, self.buf.get_end_iter())
        self.tv.scroll_mark_onscreen(self.end_mark)
        return False

    def set_status(self, text):
        self.status.set_text(text)
        return False

    def on_button(self, *_):
        if self.done:
            self.win.close()
        else:
            self.request_stop()

    def blink(self):
        if self.failed or self.done:
            self.dot.remove_css_class("off")
            return False
        if self.finalizing:
            self.dot.add_css_class("off")
            return True
        if self.dot.has_css_class("off"):
            self.dot.remove_css_class("off")
        else:
            self.dot.add_css_class("off")
        return True

    def tick(self):
        if self.started_at and not self.failed and not self.finalizing:
            self.timer.set_text(f"REC {hms(time.time() - self.started_at)}")
        return True

    # ---------------- watchdog (fail loudly) ----------------
    def watchdog(self):
        if self.failed or self.finalizing:
            return True
        if self.ffmpeg.poll() is not None:
            self.fail(f"Recorder process (ffmpeg) died, exit code "
                      f"{self.ffmpeg.returncode}. See recorder.log.")
            return False
        # is the newest chunk file actually growing?
        newest, size = self.newest_chunk()
        if newest is not None and (newest != self.w_path or size > self.w_size):
            self.w_path, self.w_size, self.w_last_growth = newest, size, time.time()
        elif time.time() - self.w_last_growth > 15:
            what = "Audio file stopped growing" if newest else "No audio file was created"
            self.fail(f"{what} — audio capture is NOT working.")
            return False
        # transcriber stuck on one chunk far longer than it should ever take
        t0 = self.transcribing_since
        if t0 and time.time() - t0 > 300:
            self.fail("Transcriber is stuck (one chunk has taken >5 min). "
                      "Audio is still being recorded to wav files.")
            return False
        return True

    def newest_chunk(self):
        try:
            names = sorted(n for n in os.listdir(self.chunk_dir) if n.endswith(".wav"))
            if not names:
                return None, -1
            p = os.path.join(self.chunk_dir, names[-1])
            return p, os.stat(p).st_size
        except OSError:
            return None, -1

    def fail(self, reason):
        if self.failed:
            return
        self.failed = True
        print(f"FAILURE: {reason}", file=sys.stderr, flush=True)
        try:
            with open(self.transcript, "a") as f:
                f.write(f"\n**[RECORDING FAILED at {hms(time.time() - self.started_at)}]** {reason}\n")
        except OSError:
            pass
        notify("⚠ MEETING RECORDING FAILED", reason, urgency="critical")
        play("alarm-clock-elapsed.oga", repeat=6)
        GLib.idle_add(self.show_failed, reason)

    def show_failed(self, reason):
        self.win.add_css_class("failed")
        self.timer.set_text("⚠ RECORDING FAILED")
        self.status.set_text(reason + f"\nPartial audio/transcript kept in {self.session}")
        self.win.present()
        return False

    def silence_check(self, peak):
        """Loud-but-recoverable warning when the audio is pure digital silence."""
        if SILENCE_CHUNKS <= 0 or self.failed:
            return
        if peak == 0:
            self.silent_streak += 1
            if self.silent_streak == SILENCE_CHUNKS and not self.silence_warn:
                self.silence_warn = True
                secs = self.silent_streak * CHUNK_SECS
                msg = (f"Last {secs}s of audio are PURE SILENCE — "
                       "check that your mic isn't muted.")
                notify("⚠ Meeting recorder hears NOTHING", msg, urgency="critical")
                play("alarm-clock-elapsed.oga", repeat=3)
                GLib.idle_add(self.show_warn, msg)
        else:
            self.silent_streak = 0
            if self.silence_warn:
                self.silence_warn = False
                GLib.idle_add(self.clear_warn)

    def show_warn(self, msg):
        self.win.add_css_class("warn")
        self.status.set_text(msg)
        self.win.present()
        return False

    def clear_warn(self):
        self.win.remove_css_class("warn")
        self.status.set_text("Audio is back — recording.")
        return False

    # ---------------- transcription ----------------
    def transcriber(self):
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(MODEL, device="cpu", compute_type="int8")
        except Exception as e:
            GLib.idle_add(lambda: (self.fail(f"Could not load whisper model: {e}"), False)[1])
            return
        self.model_ready = True
        GLib.idle_add(self.set_status, "Recording — waiting for first chunk…")

        idx = 0
        while not self.failed:
            try:
                names = sorted(n for n in os.listdir(self.chunk_dir)
                               if n.endswith(".wav"))
            except OSError:
                names = []
            # chunk idx is complete once the next chunk exists, or recording ended
            if idx < len(names) and (idx + 1 < len(names) or self.recorder_stopped):
                path = os.path.join(self.chunk_dir, names[idx])
                self.transcribing_since = time.time()
                GLib.idle_add(self.set_status,
                              f"Transcribing chunk {idx + 1}…")
                peak = wav_peak(path)
                self.silence_check(peak)
                text = ""
                try:
                    segments, _ = model.transcribe(path, language="en",
                                                   vad_filter=True, beam_size=1)
                    text = " ".join(s.text.strip() for s in segments).strip()
                except Exception as e:
                    self.transcribing_since = None
                    GLib.idle_add(lambda e=e: (self.fail(f"Transcription crashed: {e}"), False)[1])
                    return
                self.transcribing_since = None
                if text:
                    line = f"**[{hms(idx * CHUNK_SECS)}]** {text}\n\n"
                    with open(self.transcript, "a") as f:
                        f.write(line)
                    GLib.idle_add(self.append_ui, f"[{hms(idx * CHUNK_SECS)}] {text}\n")
                self.chunks_done += 1
                idx += 1
                if not self.finalizing:
                    GLib.idle_add(self.set_status,
                                  f"Live · {self.chunks_done} chunk(s) transcribed · model={MODEL}")
                continue
            if self.recorder_stopped and idx >= len(names):
                break  # drained everything
            time.sleep(0.5)

        if not self.failed:
            with open(self.transcript, "a") as f:
                f.write(f"\n*Meeting ended after {hms(time.time() - self.started_at)}.*\n")
            self.write_summary()
            GLib.idle_add(self.clean_exit)

    def write_summary(self):
        """Append an LLM summary to the transcript. Runs in transcriber thread."""
        if self.chunks_done == 0:
            return
        with open(self.transcript) as f:
            text = f.read()
        if "**[" not in text:
            return  # nothing was actually said
        GLib.idle_add(self.set_status, "Generating meeting summary…")
        summary, engine = summarize(text)
        with open(self.transcript, "a") as f:
            if summary:
                f.write(f"\n## Summary\n\n{summary}\n\n"
                        f"*Summary by {engine}.*\n")
                GLib.idle_add(self.append_ui, f"\n――― SUMMARY ―――\n{summary}\n")
            else:
                f.write("\n## Summary\n\n*SUMMARY GENERATION FAILED "
                        f"({engine}) — transcript above is complete.*\n")
        if not summary:
            print(f"SUMMARY FAILED: {engine}", file=sys.stderr, flush=True)
            notify("Meeting summary failed",
                   "Transcript is saved and complete; only the summary "
                   "could not be generated. See meeting.log.",
                   urgency="critical")

    # ---------------- stop ----------------
    def request_stop(self, *_):
        if self.finalizing or self.failed:
            if self.failed:  # allow closing a failed session
                self.clean_exit(failed=True)
            return False
        self.finalizing = True
        self.timer.set_text("Finalizing…")
        self.set_status("Stopping recorder, transcribing remaining audio…")
        threading.Thread(target=self.stop_ffmpeg, daemon=True).start()
        return False

    def stop_ffmpeg(self):
        if self.ffmpeg and self.ffmpeg.poll() is None:
            self.ffmpeg.send_signal(signal.SIGINT)
            try:
                self.ffmpeg.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.ffmpeg.kill()
        self.recorder_stopped = True

    def on_close_request(self, *_):
        if self.done:
            return False  # already saved — let the window close
        self.request_stop()
        return True  # keep window until finalized

    def clean_exit(self, failed=False):
        if failed and self.ffmpeg and self.ffmpeg.poll() is None:
            self.ffmpeg.kill()
        with open(STOPPED, "w") as f:
            f.write("failed" if failed else "clean")
        for p in (PIDFILE, FFPIDFILE):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        if failed:
            self.quit()
            return False
        play("complete.oga")
        notify("Meeting saved ✓",
               f"{self.chunks_done} chunks transcribed\n{self.transcript}")
        # stay open for reading; also open the .md in the default editor
        self.done = True
        self.dot.add_css_class("done")
        self.dot.remove_css_class("off")
        self.timer.set_text(f"Saved ✓ {hms(time.time() - self.started_at)}")
        self.set_status("Transcript saved — close this window when done reading.")
        self.stop_btn.set_label("Close window")
        subprocess.Popen(["xdg-open", self.transcript],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return False


if __name__ == "__main__":
    os.makedirs(MEETINGS, exist_ok=True)
    app = Recorder()
    app.run([])
