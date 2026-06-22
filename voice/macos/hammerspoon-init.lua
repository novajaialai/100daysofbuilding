-- AI Pilled Voice (macOS) — push-to-talk dictation controller.
-- Karabiner maps a TAP of Right-Command -> F18; this binds F18 to toggle dictation.
-- Mirrors the Linux Super+Z tool: record -> faster-whisper -> type text + Return.

local HOME    = os.getenv("HOME")
local SCRIPT  = HOME .. "/voice/voice-mac.sh"
local SOUNDS  = "/System/Library/Sounds/"
local recording = false
local busy      = false   -- true while a transcription is in flight

local function beep(name)
  hs.task.new("/usr/bin/afplay", nil, { SOUNDS .. name .. ".aiff" }):start()
end

-- run voice-mac.sh {start|stop} with a brew-aware PATH; cb(stdout, stderr)
local function run(arg, cb)
  local t = hs.task.new("/bin/bash", function(_code, out, err) cb(out or "", err or "") end,
                        { SCRIPT, arg })
  t:setEnvironment({ HOME = HOME, PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" })
  t:start()
end

local function startDictation()
  recording = true
  beep("Tink")
  hs.alert.closeAll()
  hs.alert.show("🎙  Listening…  (tap Right-⌘ to send)", 1.2)
  run("start", function(out, _err)
    if out:find("__MIC_ERROR__") then
      recording = false
      hs.alert.show("Mic error — check Microphone permission for Hammerspoon")
    end
  end)
end

local function stopDictation()
  recording = false
  beep("Pop")
  hs.alert.show("✍️  Transcribing…", 0.8)
  busy = true
  run("stop", function(out, _err)
    busy = false
    local text = out
    if text == "" then hs.alert.show("Heard nothing"); return end
    if text:find("__TOO_LONG__") then
      hs.alert.show("Refused: transcript too long (likely a misfire)"); return
    end
    -- terminal-submit mode: type where the cursor is, then press Return
    hs.eventtap.keyStrokes(text)
    hs.timer.doAfter(0.12, function()
      hs.eventtap.keyStroke({}, "return", 0)
      beep("Glass")
    end)
  end)
end

local function toggle()
  if busy then return end   -- ignore taps while transcribing
  if recording then stopDictation() else startDictation() end
end

-- F18 is emitted by Karabiner when Right-Command is tapped alone.
hs.hotkey.bind({}, "f18", toggle)

hs.alert.show("Voice dictation loaded ✓  (tap Right-⌘ to dictate)")
