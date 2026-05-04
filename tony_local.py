#!/usr/bin/env python3
"""
tony_local.py — Tony classroom mic listener.

Flow:
  1. VAD + tiny Whisper listen for Tony's name.
  2. On hearing name → perk reaction, enter recording mode.
  3. Collect all speech until 2.5 s of silence (no "thank you" required).
  4. Batch-transcribe with base Whisper → post to #tonys-chat-room with @professor.
"""
import json, os, queue, threading, time, subprocess
import numpy as np
import whisper
import requests
from scipy.signal import resample_poly
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "discord_bot", ".env"))

# ── Config ────────────────────────────────────────────────────────────
ALSA_DEVICE  = "hw:2,0"
CAPTURE_RATE = 44100
CHANNELS     = 1

# VAD settings
VAD_CHUNK    = 0.1          # seconds per energy check (100ms)
VAD_ON_RMS   = 800          # RMS above this = speech started  (mic peak ~1379, bg ~405)
VAD_OFF_RMS  = 550          # RMS below this = speech ended
VAD_OFF_SECS = 0.5          # silence this long after speech = burst complete
VAD_MAX_SECS = 6.0          # max burst length before forcing a flush

# Recording settings
SILENCE_TIMEOUT = 2.5       # seconds of no new burst = question is done
MAX_RECORD_SECS = 60.0      # hard cap on recording length
MIN_QUESTION_LEN = 6        # minimum word count to post (avoids false triggers)

# Wake word — common Whisper mishearings of "Tony" included
WAKE_VARIANTS = {"tony", "toni", "toby", "tony's", "tonys", "tone"}

CMD_FILE = "/tmp/tony_cmd.json"

DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN", "")
QUESTIONS_CH     = int(os.getenv("QUESTIONS_CHANNEL_ID", "1470571362287358090"))
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL       = os.getenv("GROQ_MODEL", "llama3-8b-8192")

_prof_env        = os.getenv("PROFESSOR_IDS", "182644353137770497")
PROF_MENTIONS    = " ".join(f"<@{p}>" for p in _prof_env.split(",") if p.strip())

DISCORD_API = "https://discord.com/api/v10"
HEADERS     = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}

# ── Creature command ──────────────────────────────────────────────────
def creature_cmd(cmd: str):
    try:
        with open(CMD_FILE, "w") as f:
            json.dump({"cmd": cmd, "ts": time.time()}, f)
    except Exception:
        pass

# ── Whisper ───────────────────────────────────────────────────────────
print("[Tony] Loading Whisper models…")
_whisper_fast = whisper.load_model("tiny")   # hotword detection
_whisper_full = whisper.load_model("base")   # question transcription
print("[Tony] Whisper ready.")

_OPTS_FAST = dict(language="en", fp16=False,
                  no_speech_threshold=0.7,
                  logprob_threshold=-0.7,
                  compression_ratio_threshold=1.8,
                  condition_on_previous_text=False)
_OPTS_FULL = dict(language="en", fp16=False,
                  no_speech_threshold=0.5,
                  logprob_threshold=-0.9,
                  compression_ratio_threshold=2.0,
                  condition_on_previous_text=True,
                  initial_prompt="English classroom. A student is asking a question.")

NO_SPEECH_PROB_LIMIT = 0.7  # skip if Whisper thinks this is mostly non-speech

def transcribe(pcm: np.ndarray, fast: bool) -> str:
    audio  = pcm.astype(np.float32) / 32768.0
    audio  = resample_poly(audio, 160, 441).astype(np.float32)
    model  = _whisper_fast if fast else _whisper_full
    opts   = _OPTS_FAST if fast else _OPTS_FULL
    result = model.transcribe(audio, **opts)

    segments = result.get("segments", [])
    if segments:
        avg_no_speech = sum(s["no_speech_prob"] for s in segments) / len(segments)
        if avg_no_speech > NO_SPEECH_PROB_LIMIT:
            return ""

    return result["text"].strip().lower()

# ── Discord ───────────────────────────────────────────────────────────
def discord_post(channel_id: int, text: str):
    try:
        requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=HEADERS, json={"content": text}, timeout=10,
        )
    except Exception as e:
        print(f"[Tony] Discord error: {e}")

# ── Wake word helper ──────────────────────────────────────────────────
import string as _string

def _words(text: str) -> set:
    return {w.strip(_string.punctuation) for w in text.split()}

def has_wake(text: str) -> bool:
    return bool(_words(text) & WAKE_VARIANTS)

# ── VAD burst collector ───────────────────────────────────────────────
VAD_CHUNK_BYTES = int(CAPTURE_RATE * CHANNELS * 2 * VAD_CHUNK)
VAD_OFF_CHUNKS  = int(VAD_OFF_SECS / VAD_CHUNK)
VAD_MAX_CHUNKS  = int(VAD_MAX_SECS / VAD_CHUNK)

_burst_q: queue.Queue = queue.Queue()

def _vad_loop(proc):
    buf          = []
    silent_ticks = 0
    speaking     = False

    while True:
        raw = proc.stdout.read(VAD_CHUNK_BYTES)
        if len(raw) < VAD_CHUNK_BYTES // 2:
            continue
        chunk = np.frombuffer(raw, dtype=np.int16)
        rms   = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

        if not speaking:
            if rms >= VAD_ON_RMS:
                speaking     = True
                silent_ticks = 0
                buf          = [chunk]
        else:
            buf.append(chunk)
            if rms < VAD_OFF_RMS:
                silent_ticks += 1
                if silent_ticks >= VAD_OFF_CHUNKS:
                    _burst_q.put(np.concatenate(buf))
                    buf = []; speaking = False; silent_ticks = 0
            else:
                silent_ticks = 0
                if len(buf) >= VAD_MAX_CHUNKS:
                    _burst_q.put(np.concatenate(buf))
                    buf = []

# ── Main ──────────────────────────────────────────────────────────────
def main():
    print(f"[Tony] Opening mic {ALSA_DEVICE} at {CAPTURE_RATE} Hz…")
    proc = subprocess.Popen(
        ["arecord", "-D", ALSA_DEVICE, "-f", "S16_LE",
         "-r", str(CAPTURE_RATE), "-c", str(CHANNELS), "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    threading.Thread(target=_vad_loop, args=(proc,), daemon=True).start()
    print("[Tony] Listening for 'Tony'…")

    state           = "idle"
    prev_text       = ""
    question_chunks = []   # list of np.ndarray bursts collected during recording
    last_burst_time = 0.0
    record_start    = 0.0

    try:
        while True:

            # ── Phase 1: idle — wait for wake word ────────────────────
            if state == "idle":
                try:
                    burst = _burst_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                text = transcribe(burst, fast=True)
                if not text:
                    continue
                print(f"[mic] {text}")

                window = (prev_text + " " + text).strip()
                if has_wake(window):
                    state           = "recording"
                    question_chunks = [burst]   # wake burst may contain the question
                    last_burst_time = time.time()
                    record_start    = time.time()
                    prev_text       = ""
                    creature_cmd("perk")
                    print("[Tony] Heard name — listening for question…")
                else:
                    prev_text = text

            # ── Phase 2: recording — collect until silence ────────────
            elif state == "recording":
                try:
                    burst = _burst_q.get(timeout=0.3)
                    question_chunks.append(burst)
                    last_burst_time = time.time()
                except queue.Empty:
                    pass

                now             = time.time()
                silence_elapsed = now - last_burst_time
                record_elapsed  = now - record_start

                done = (silence_elapsed >= SILENCE_TIMEOUT or
                        record_elapsed  >= MAX_RECORD_SECS)

                if not done:
                    continue

                # ── Transcribe & post ─────────────────────────────────
                state     = "idle"
                prev_text = ""

                if not question_chunks:
                    creature_cmd("idle")
                    continue

                print("[Tony] Transcribing question…")
                creature_cmd("loading")
                all_audio = np.concatenate(question_chunks)
                question  = transcribe(all_audio, fast=False).strip(" .,!?")

                print(f"[Tony] Question: {question}")

                word_count = len(question.split())
                if word_count < MIN_QUESTION_LEN:
                    print(f"[Tony] Too short ({word_count} words) — ignoring.")
                    creature_cmd("idle")
                    continue

                discord_post(
                    QUESTIONS_CH,
                    f"🎙️ **Student question:**\n> {question}\n\n"
                    f"{PROF_MENTIONS} — please reply to this message with your answer."
                )
                print("[Tony] Posted to Discord.")
                time.sleep(2)
                creature_cmd("idle")

    finally:
        proc.terminate()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Tony] Stopped.")
