#!/usr/bin/env python3
"""Call & Meeting Recorder supervisor.

Records your microphone AND the system audio (the other side of any call,
video meeting, or phone call routed through the laptop) as TWO separate,
time-synced streams, so the live transcript can label who said what
("You" vs "Them"). Each stream is segmented into short wav chunks and
live-transcribed with faster-whisper into a timestamped markdown transcript.
Shows a GTK window with a blinking REC dot, elapsed time, live per-channel
audio level meters, and the live speaker-labeled transcript.

Fails LOUDLY: an internal watchdog checks every 2s that the recorder process
is alive, BOTH audio streams are actually growing, the mic isn't pure digital
silence, the transcriber isn't stuck, and the disk isn't about to fill (the
real silent-loss risk on a long recording). Any failure -> red window, alarm
sound, critical notification. An external watchdog (watchdog.sh) alarms if
this whole process dies, and meeting.sh runs us under systemd-inhibit so the
machine can't sleep or lid-suspend mid-call.
"""
import datetime
import os
import shutil
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
# N consecutive all-silent mic chunks => loud "your mic hears nothing" warning.
SILENCE_CHUNKS = int(os.environ.get("MEETING_SILENCE_CHUNKS", "3"))
# Speaker labels. Mic = you; system audio = whoever is on the other end.
SPK_MIC = os.environ.get("MEETING_SPEAKER_MIC", "You")
SPK_SYS = os.environ.get("MEETING_SPEAKER_SYS", "Them")
# Peak (0..32767) below which a chunk is treated as silence and NOT sent to
# whisper — halves CPU on a normal call (one person talks at a time) and stops
# whisper hallucinating words onto room tone. Keeps dual-stream realtime-safe.
SILENCE_PEAK = int(os.environ.get("MEETING_SILENCE_PEAK", "60"))
# Disk thresholds (bytes). Warn loudly when low, fail before we actually fill.
DISK_WARN = int(os.environ.get("MEETING_DISK_WARN_MB", "2048")) * 1024 * 1024
DISK_FAIL = int(os.environ.get("MEETING_DISK_FAIL_MB", "400")) * 1024 * 1024
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
PIDFILE = os.path.join(RUNTIME, "meeting-rec.pid")
FFPIDFILE = os.path.join(RUNTIME, "meeting-rec.ffmpeg.pid")
INHPIDFILE = os.path.join(RUNTIME, "meeting-rec.inhibit.pid")
STOPPED = os.path.join(RUNTIME, "meeting-rec.stopped")
SND = "/usr/share/sounds/freedesktop/stereo"
MEETINGS = os.path.expanduser("~/Meetings")

CSS = """
window { background-color: #0f1216; }
window.warn { background-color: #5c3a00; }
window.failed { background-color: #6e1010; }
label { color: #e6e9ee; }
.rec-dot { color: #ff3b30; font-size: 22px; }
.rec-dot.off { color: #2c313a; }
.rec-dot.done { color: #34c759; }
.rec-title { font-size: 18px; font-weight: bold; }
.status { color: #9aa3ad; font-size: 12px; }
window.failed .status, window.warn .status {
  color: #ffffff; font-size: 15px; font-weight: bold;
}
.path { color: #6f7882; font-size: 11px; }
.meter-label { font-size: 11px; font-weight: bold; }
.meter-label.you { color: #5aa9ff; }
.meter-label.them { color: #34c759; }
levelbar trough { background-color: #0c0e11; border-radius: 4px; min-height: 9px; }
levelbar.you block.filled { background-color: #5aa9ff; border-radius: 4px; }
levelbar.them block.filled { background-color: #34c759; border-radius: 4px; }
levelbar block.empty { background-color: #0c0e11; }
textview, textview text { background-color: #0c0e11; color: #cfd6dd; }
"""


def notify(summary, body="", urgency="normal"):
    subprocess.Popen(
        ["notify-send", "-a", "Call & Meeting Recorder", "-u", urgency, summary, body],
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


def tail_level(path, want_bytes=12000):
    """Normalised (0..1) peak of the last ~fraction of a second of a growing
    wav file — drives the live level meter. Reads raw PCM past the 44-byte
    header; misalignment by a byte is harmless."""
    try:
        import numpy as np
        sz = os.stat(path).st_size
        avail = sz - 44
        if avail <= 0:
            return 0.0
        n = min(avail, want_bytes)
        n -= n % 2
        with open(path, "rb") as f:
            f.seek(sz - n)
            raw = f.read(n)
        a = np.frombuffer(raw, dtype=np.int16)
        if not a.size:
            return 0.0
        return min(1.0, (float(np.abs(a).max()) / 32768.0) * 3.2)
    except Exception:
        return 0.0


SUMMARY_PROMPT = (
    "Below is a call/meeting transcript with timestamps and speaker labels "
    f"('{SPK_MIC}' is the person recording, '{SPK_SYS}' is the other side). "
    "Write a concise summary: 2-4 sentences on what the conversation was about, "
    "then bullet lists for key points, decisions made, and action items (omit a "
    "list if there are none). Output plain markdown, no preamble.\n\nTRANSCRIPT:\n")


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


def mmss(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class Recorder(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.jake.call-meeting-recorder",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.failed = False
        self.finalizing = False
        self.done = False
        self.recorder_stopped = False
        self.silence_warn = False
        self.disk_warn = False
        self.started_at = None
        self.ffmpeg = None
        self.chunks_done = 0
        self.silent_streak = 0
        self.transcribing_since = None
        self.model_ready = False
        self.inhibitor = None
        # watchdog state: "are BOTH audio streams growing" (keyed by dir)
        self.w_path = {}
        self.w_size = {}
        self.w_last_growth = {}

    # ---------------- startup ----------------
    def do_activate(self):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session = os.path.join(MEETINGS, stamp)
        self.mic_dir = os.path.join(self.session, "mic")
        self.sys_dir = os.path.join(self.session, "sys")
        os.makedirs(self.mic_dir, exist_ok=True)
        os.makedirs(self.sys_dir, exist_ok=True)
        self.transcript = os.path.join(self.session, "transcript.md")
        with open(self.transcript, "w") as f:
            f.write(f"# Call / Meeting {stamp}\n\n"
                    f"*{SPK_MIC} = mic · {SPK_SYS} = other side (system audio)*\n\n")
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

        # One ffmpeg, two pulse inputs, two segmented outputs that cut at the
        # same wall-clock instants — so chunk i in mic/ and sys/ cover the same
        # window and can be merged by timestamp.
        seg = ["-f", "segment", "-segment_time", str(CHUNK_SECS),
               "-reset_timestamps", "1"]
        enc = ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-f", "pulse", "-i", "default",            # 0: microphone (You)
               "-f", "pulse", "-i", f"{sink}.monitor",    # 1: system (Them)
               "-map", "0:a", *enc, *seg,
               os.path.join(self.mic_dir, "chunk_%05d.wav"),
               "-map", "1:a", *enc, *seg,
               os.path.join(self.sys_dir, "chunk_%05d.wav")]
        self.rec_log = open(os.path.join(self.session, "recorder.log"), "w")
        try:
            self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                           stdout=self.rec_log, stderr=self.rec_log)
        except Exception as e:
            self.startup_fail(f"Could not start ffmpeg recorder: {e}")
            return

        self.build_window()
        self.started_at = time.time()
        now = time.time()
        for d in (self.mic_dir, self.sys_dir):
            self.w_path[d] = None
            self.w_size[d] = -1
            self.w_last_growth[d] = now

        with open(FFPIDFILE, "w") as f:
            f.write(str(self.ffmpeg.pid))
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
        try:
            os.unlink(STOPPED)
        except FileNotFoundError:
            pass
        self.acquire_inhibit()

        threading.Thread(target=self.transcriber, daemon=True).start()
        GLib.timeout_add(600, self.blink)
        GLib.timeout_add(1000, self.tick)
        GLib.timeout_add(180, self.update_meters)
        GLib.timeout_add(2000, self.watchdog)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, self.request_stop)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.request_stop)
        play("dialog-information.oga")
        notify("Recording call / meeting", f"Transcript: {self.transcript}")

    # ---------------- suspend inhibitor ----------------
    def acquire_inhibit(self):
        """Block sleep/idle/lid-suspend for the LIFETIME OF RECORDING only.
        Held by a child `systemd-inhibit … sleep infinity`; released the instant
        recording stops, even while the saved-transcript window stays open."""
        try:
            self.inhibitor = subprocess.Popen(
                ["systemd-inhibit", "--what=sleep:idle:handle-lid-switch",
                 "--who=Call & Meeting Recorder", "--why=Recording in progress",
                 "--mode=block", "sleep", "infinity"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            with open(INHPIDFILE, "w") as f:
                f.write(str(self.inhibitor.pid))
        except Exception as e:
            # Non-fatal: recording still works, the machine just isn't pinned awake.
            self.inhibitor = None
            print(f"WARN: could not inhibit suspend: {e}", file=sys.stderr, flush=True)
            notify("Recorder: could not block sleep",
                   "Recording anyway — keep the laptop awake during long calls.")

    def release_inhibit(self):
        if self.inhibitor and self.inhibitor.poll() is None:
            try:
                self.inhibitor.terminate()
            except Exception:
                pass
        self.inhibitor = None
        try:
            os.unlink(INHPIDFILE)
        except FileNotFoundError:
            pass

    def startup_fail(self, reason):
        print(f"STARTUP FAILURE: {reason}", file=sys.stderr, flush=True)
        notify("⚠ RECORDER FAILED TO START", reason, urgency="critical")
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

        self.win = Gtk.ApplicationWindow(application=self,
                                         title="Call & Meeting Recorder")
        self.win.set_default_size(520, 480)
        self.win.connect("close-request", self.on_close_request)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12,
                      margin_start=14, margin_end=14)

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

        # Live per-channel level meters — proof both sides are being captured.
        self.lvl_mic = self._meter(box, SPK_MIC, "you")
        self.lvl_sys = self._meter(box, SPK_SYS, "them")

        self.status = Gtk.Label(label="Loading transcription model…", xalign=0)
        self.status.add_css_class("status")
        self.status.set_wrap(True)
        box.append(self.status)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.tv = Gtk.TextView(editable=False, cursor_visible=False,
                               wrap_mode=Gtk.WrapMode.WORD_CHAR,
                               left_margin=8, right_margin=8,
                               top_margin=6, bottom_margin=6)
        self.buf = self.tv.get_buffer()
        self.tag_you = self.buf.create_tag("you", foreground="#5aa9ff",
                                           weight=700)
        self.tag_them = self.buf.create_tag("them", foreground="#34c759",
                                            weight=700)
        self.tag_time = self.buf.create_tag("time", foreground="#6f7882")
        self.end_mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
        scroll.set_child(self.tv)
        box.append(scroll)

        path_lbl = Gtk.Label(label=self.transcript, xalign=0, selectable=True)
        path_lbl.add_css_class("path")
        path_lbl.set_ellipsize(3)  # Pango.EllipsizeMode.END
        box.append(path_lbl)

        self.win.set_child(box)
        self.win.present()

    def _meter(self, box, name, kind):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=name, xalign=0)
        lbl.add_css_class("meter-label")
        lbl.add_css_class(kind)
        lbl.set_size_request(54, -1)
        bar = Gtk.LevelBar(min_value=0.0, max_value=1.0, value=0.0,
                           mode=Gtk.LevelBarMode.CONTINUOUS, hexpand=True)
        bar.add_css_class(kind)
        # Suppress the default "high/full" recolouring so our CSS colour wins.
        for off in ("low", "high", "full"):
            bar.add_offset_value(off, 1.0)
        bar.set_valign(Gtk.Align.CENTER)
        row.append(lbl)
        row.append(bar)
        box.append(row)
        return bar

    def update_meters(self):
        if self.failed or self.finalizing or self.done:
            self.lvl_mic.set_value(0.0)
            self.lvl_sys.set_value(0.0)
            return not (self.failed or self.done)
        self.lvl_mic.set_value(self._tail(self.mic_dir))
        self.lvl_sys.set_value(self._tail(self.sys_dir))
        return True

    def _tail(self, d):
        p, _ = self.newest_chunk(d)
        return tail_level(p) if p else 0.0

    def append_seg(self, speaker, t, text):
        kind = "you" if speaker == SPK_MIC else "them"
        tag = self.tag_you if kind == "you" else self.tag_them
        it = self.buf.get_end_iter()
        self.buf.insert_with_tags(it, f"[{mmss(t)}] ", self.tag_time)
        self.buf.insert_with_tags(self.buf.get_end_iter(), f"{speaker}: ", tag)
        self.buf.insert(self.buf.get_end_iter(), f"{text}\n")
        self.buf.move_mark(self.end_mark, self.buf.get_end_iter())
        self.tv.scroll_mark_onscreen(self.end_mark)
        return False

    def append_plain(self, line):
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
        # are BOTH audio streams actually growing? (mic dead, or sink changed)
        now = time.time()
        for d, who in ((self.mic_dir, "Microphone"), (self.sys_dir, "System audio")):
            newest, size = self.newest_chunk(d)
            # progress = a new chunk file appeared OR the current one grew.
            # (a fresh segment file resets small, so size alone isn't enough)
            if newest is not None and (newest != self.w_path[d]
                                       or size > self.w_size[d]):
                self.w_path[d] = newest
                self.w_size[d] = size
                self.w_last_growth[d] = now
            elif now - self.w_last_growth[d] > 15:
                what = (f"{who} stream stopped growing" if newest
                        else f"No {who.lower()} file was created")
                self.fail(f"{what} — audio capture is NOT working.")
                return False
        # disk about to fill = silent data loss on a long recording
        try:
            free = shutil.disk_usage(self.session).free
        except OSError:
            free = None
        if free is not None:
            if free < DISK_FAIL:
                self.fail(f"Disk almost full ({free // (1024*1024)} MB left). "
                          "Stopping before the recording is corrupted.")
                return False
            if free < DISK_WARN and not self.disk_warn:
                self.disk_warn = True
                msg = (f"Low disk: {free // (1024*1024)} MB left. "
                       "Free space or the long recording may not fit.")
                notify("⚠ Recorder: low disk space", msg, urgency="critical")
                play("alarm-clock-elapsed.oga", repeat=2)
                GLib.idle_add(self.show_warn, msg)
            elif free >= DISK_WARN and self.disk_warn and not self.silence_warn:
                self.disk_warn = False
                GLib.idle_add(self.clear_warn)
        # transcriber stuck on one chunk far longer than it should ever take
        t0 = self.transcribing_since
        if t0 and now - t0 > 300:
            self.fail("Transcriber is stuck (one chunk has taken >5 min). "
                      "Audio is still being recorded to wav files.")
            return False
        return True

    def newest_chunk(self, d):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".wav"))
            if not names:
                return None, -1
            p = os.path.join(d, names[-1])
            return p, os.stat(p).st_size
        except OSError:
            return None, -1

    def fail(self, reason):
        if self.failed:
            return
        self.failed = True
        self.release_inhibit()
        print(f"FAILURE: {reason}", file=sys.stderr, flush=True)
        try:
            with open(self.transcript, "a") as f:
                f.write(f"\n**[RECORDING FAILED at {hms(time.time() - self.started_at)}]** {reason}\n")
        except OSError:
            pass
        notify("⚠ RECORDING FAILED", reason, urgency="critical")
        play("alarm-clock-elapsed.oga", repeat=6)
        GLib.idle_add(self.show_failed, reason)

    def show_failed(self, reason):
        self.win.add_css_class("failed")
        self.timer.set_text("⚠ RECORDING FAILED")
        self.status.set_text(reason + f"\nPartial audio/transcript kept in {self.session}")
        self.win.present()
        return False

    def silence_check(self, peak):
        """Loud-but-recoverable warning when the MIC is pure digital silence —
        i.e. your side isn't being captured (mic muted/unplugged)."""
        if SILENCE_CHUNKS <= 0 or self.failed:
            return
        if peak == 0:
            self.silent_streak += 1
            if self.silent_streak == SILENCE_CHUNKS and not self.silence_warn:
                self.silence_warn = True
                secs = self.silent_streak * CHUNK_SECS
                msg = (f"Last {secs}s from your MIC are PURE SILENCE — "
                       "check that your mic isn't muted or unplugged.")
                notify("⚠ Recorder hears nothing from your mic", msg,
                       urgency="critical")
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
            mic_names = self._names(self.mic_dir)
            sys_names = self._names(self.sys_dir)
            have = min(len(mic_names), len(sys_names))
            # chunk idx is complete once the NEXT chunk exists in both streams,
            # or the recorder has stopped (final partial chunk).
            ready = idx < have and (idx + 1 < have or self.recorder_stopped)
            if ready:
                self.transcribing_since = time.time()
                backlog = max(0, have - 1 - idx)
                GLib.idle_add(self.set_status,
                              f"Transcribing chunk {idx + 1}…"
                              + (f"  ({backlog} behind)" if backlog else ""))
                events = self.transcribe_pair(model, mic_names[idx], sys_names[idx],
                                              idx)
                if events is None:
                    return  # fail() already fired
                self.write_events(events)
                self.transcribing_since = None
                self.chunks_done += 1
                idx += 1
                if not self.finalizing:
                    GLib.idle_add(self.set_status,
                                  f"Live · {self.chunks_done} chunk(s) · "
                                  f"model={MODEL}"
                                  + (f" · {backlog} behind" if backlog else ""))
                continue
            if self.recorder_stopped and idx >= have:
                break  # drained everything
            time.sleep(0.5)

        if not self.failed:
            with open(self.transcript, "a") as f:
                f.write(f"\n*Ended after {hms(time.time() - self.started_at)}.*\n")
            self.write_summary()
            GLib.idle_add(self.clean_exit)

    def _names(self, d):
        try:
            return sorted(n for n in os.listdir(d) if n.endswith(".wav"))
        except OSError:
            return []

    def transcribe_pair(self, model, mic_name, sys_name, idx):
        """Transcribe the mic and system chunk for one window, returning a
        time-ordered, speaker-coalesced list of (abs_secs, speaker, text).
        Skips a channel that's silent (saves CPU, avoids hallucination).
        Returns None if transcription crashed (after firing fail())."""
        base = idx * CHUNK_SECS
        events = []
        mic_peak = wav_peak(os.path.join(self.mic_dir, mic_name))
        self.silence_check(mic_peak)
        for path, speaker, peak in (
                (os.path.join(self.mic_dir, mic_name), SPK_MIC, mic_peak),
                (os.path.join(self.sys_dir, sys_name), SPK_SYS,
                 wav_peak(os.path.join(self.sys_dir, sys_name)))):
            if peak < SILENCE_PEAK:  # silence or unreadable -> nothing said
                continue
            try:
                segments, _ = model.transcribe(path, language="en",
                                                vad_filter=True, beam_size=1)
                for s in segments:
                    t = s.text.strip()
                    if t:
                        events.append((base + s.start, speaker, t))
            except Exception as e:
                self.transcribing_since = None
                GLib.idle_add(lambda e=e: (self.fail(f"Transcription crashed: {e}"), False)[1])
                return None
        events.sort(key=lambda e: e[0])
        # coalesce consecutive segments from the same speaker into one line
        merged = []
        for t, spk, txt in events:
            if merged and merged[-1][1] == spk:
                merged[-1][2] += " " + txt
            else:
                merged.append([t, spk, txt])
        return merged

    def write_events(self, events):
        if not events:
            return
        with open(self.transcript, "a") as f:
            for t, spk, txt in events:
                f.write(f"**[{mmss(t)}] {spk}:** {txt}\n\n")
                GLib.idle_add(self.append_seg, spk, t, txt)

    def write_summary(self):
        """Append an LLM summary to the transcript. Runs in transcriber thread."""
        if self.chunks_done == 0:
            return
        with open(self.transcript) as f:
            text = f.read()
        if "**[" not in text:
            return  # nothing was actually said
        GLib.idle_add(self.set_status, "Generating summary…")
        summary, engine = summarize(text)
        with open(self.transcript, "a") as f:
            if summary:
                f.write(f"\n## Summary\n\n{summary}\n\n"
                        f"*Summary by {engine}.*\n")
                GLib.idle_add(self.append_plain, f"\n――― SUMMARY ―――\n{summary}\n")
            else:
                f.write("\n## Summary\n\n*SUMMARY GENERATION FAILED "
                        f"({engine}) — transcript above is complete.*\n")
        if not summary:
            print(f"SUMMARY FAILED: {engine}", file=sys.stderr, flush=True)
            notify("Summary failed",
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
        self.release_inhibit()  # capture is done — let the machine sleep again

    def on_close_request(self, *_):
        if self.done:
            return False  # already saved — let the window close
        self.request_stop()
        return True  # keep window until finalized

    def clean_exit(self, failed=False):
        if failed and self.ffmpeg and self.ffmpeg.poll() is None:
            self.ffmpeg.kill()
        self.release_inhibit()
        with open(STOPPED, "w") as f:
            f.write("failed" if failed else "clean")
        for p in (PIDFILE, FFPIDFILE, INHPIDFILE):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        if failed:
            self.quit()
            return False
        play("complete.oga")
        notify("Saved ✓",
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
