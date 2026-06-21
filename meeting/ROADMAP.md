# Call & Meeting Recorder — Roadmap

Where the recording capability started, where it is now, and where it's going —
with a focus on the question that drives it: **can we record the *other end* of a
call?**

## Where we started (recorder v1)

Super+R → one ffmpeg process capturing **mic + system audio mixed to a single mono
track**, a single transcript with **no speaker labels**, and a basic watchdog. Good
enough to capture a meeting, but you couldn't tell who said what, and a phone call
that lived on a handset was out of reach. (The early sessions in `~/Meetings/` are
from this version.)

## Where we are now (recorder v2)

Rebuilt to capture **both sides separately**:

- **Dual-stream** — records the **mic** (you) and the **far-end audio** as two
  time-synced 16 kHz streams (`mic/` + `sys/`), transcribes each, and labels the
  transcript **You** vs **Them**.
- **Live level meters** for both channels, so you can see both sides are landing.
- **Built for long calls** — holds a sleep/idle/lid inhibit for the recording's
  lifetime, a disk-space watchdog that stops cleanly before the disk fills, and
  unbounded chunked-to-disk length.
- **Auto transcribe + summary** — faster-whisper live, then an LLM summary on stop.
- (Sibling tools: Super+Z/A voice dictation in; Super+X read-aloud now on local
  **Kokoro** neural TTS.)

> Honest status: the dual-stream code is in place and tested, but no *real* call
> session on disk has exercised it end-to-end yet — the first real recording is the
> validation.

### Can we record the other end?

| Call type | How the far end is captured | Status |
|---|---|---|
| Video meeting (Zoom / Meet / Teams) | the default sink's `.monitor` | ✅ works now |
| Softphone / VoIP / browser dialer | the default sink's `.monitor` | ✅ works now |
| **Actual cellphone call on a handset** | phone audio must be *bridged into* the laptop | ⏳ in progress |

The far end of anything that plays through the laptop's speakers is already captured
as **Them**. The only gap is a real cellphone call, whose audio never touches the
laptop.

## Where it's going

1. **Cellphone capture — selectable far-end source** *(building now)*
   The recorder's far-end input is no longer hard-wired to the system monitor. Set
   `MEETING_FAR_SOURCE` (or run `meeting.sh phone`) to record a phone bridged in over
   **Bluetooth (HFP)** or a **line-in / USB** device as **Them**:
   - `meeting.sh sources` — list audio inputs, annotated, to find the phone's source.
   - `meeting.sh phone` — auto-pick a Bluetooth/line-in source and start recording.
   - `MEETING_FAR_SOURCE` / `MEETING_MIC_SOURCE` — set either input explicitly;
     a bad name fails loudly at startup instead of recording silence.
   **Closes the cellular gap** once validated against a real paired-phone call.
2. **First real long-call validation** end-to-end; optional larger whisper model for
   tougher audio.
3. **Far-end diarization** — distinguish multiple people who currently share the
   "Them" label.
4. **Knowledge-base ingestion** — file transcripts + summaries into the Claude-Jake-OS
   KB so the dashboard surfaces every call automatically.

See `README.md` for usage and `supervisor.py` for the implementation.
