#!/usr/bin/env python3
"""
tony_local.py — Tony classroom mic listener.

Two-phase listening:
  Phase 1 (hotword): VAD collects natural speech bursts, tiny Whisper checks
                     for "tony" — no Whisper runs on silence or background noise.
  Phase 2 (question): After wake word confirmed, base Whisper transcribes the
                      full question until "thank you".
"""
import json, os, queue, threading, time, subprocess
import numpy as np
import whisper
import requests
from scipy.signal import resample_poly
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "discord_bot", ".env"))

# ── Config ───────────────────────────────────────────────────────────
ALSA_DEVICE  = "hw:2,0"
CAPTURE_RATE = 44100
CHANNELS     = 1

# VAD settings
VAD_CHUNK    = 0.1          # seconds per energy check (100ms)
VAD_ON_RMS   = 5000         # RMS above this = speech started (classroom ambient ~1673)
VAD_OFF_RMS  = 2500         # RMS below this = speech ended
VAD_OFF_SECS = 0.5          # silence this long after speech = burst complete
VAD_MAX_SECS = 6.0          # max burst length before forcing transcription

# Wake word — common Whisper mishearings of "Tony" included
WAKE_VARIANTS  = {"tony", "toni", "toby", "tony's", "tonys", "tone"}
QUESTION_WORDS = {"question", "ask", "help", "what", "why", "how",
                  "can", "could", "would", "explain", "tell", "show"}
STOP_PHRASE    = "thank you"

CMD_FILE = "/tmp/tony_cmd.json"

DISCORD_TOKEN     = os.getenv("DISCORD_TOKEN", "")
QUESTIONS_CHANNEL = int(os.getenv("QUESTIONS_CHANNEL_ID", "1470571362287358090"))
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL        = os.getenv("GROQ_MODEL", "llama3-8b-8192")

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

_OPTS = dict(
    language="en",
    fp16=False,
    condition_on_previous_text=False,
    no_speech_threshold=0.6,
    initial_prompt="English classroom.",
)

def transcribe(pcm: np.ndarray, fast: bool) -> str:
    audio  = pcm.astype(np.float32) / 32768.0
    audio  = resample_poly(audio, 160, 441).astype(np.float32)
    model  = _whisper_fast if fast else _whisper_full
    result = model.transcribe(audio, **_OPTS)
    return result["text"].strip().lower()

# ── Discord / Groq ────────────────────────────────────────────────────
def discord_post(text: str):
    try:
        requests.post(
            f"{DISCORD_API}/channels/{QUESTIONS_CHANNEL}/messages",
            headers=HEADERS, json={"content": text}, timeout=10,
        )
    except Exception:
        pass

from groq import Groq
_groq = Groq(api_key=GROQ_API_KEY)

def ask_groq(question: str) -> str:
    resp = _groq.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": "You are Tony, a classroom support robot. Answer clearly and concisely."},
            {"role": "user",   "content": question},
        ],
        max_tokens=400,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()

# ── Wake word helpers ─────────────────────────────────────────────────
import string as _string

def _words(text: str) -> set:
    return {w.strip(_string.punctuation) for w in text.split()}

def has_wake(text: str) -> bool:
    return bool(_words(text) & WAKE_VARIANTS)

def has_question_word(text: str) -> bool:
    return bool(_words(text) & QUESTION_WORDS)

# ── VAD burst collector ───────────────────────────────────────────────
# Yields complete speech bursts as np.ndarray (int16) ready for Whisper.
VAD_CHUNK_BYTES = int(CAPTURE_RATE * CHANNELS * 2 * VAD_CHUNK)
VAD_OFF_CHUNKS  = int(VAD_OFF_SECS / VAD_CHUNK)
VAD_MAX_CHUNKS  = int(VAD_MAX_SECS / VAD_CHUNK)

_burst_q = queue.Queue()

def _vad_loop(proc):
    """Read mic in 100ms ticks. Collect speech bursts and push to _burst_q."""
    buf          = []
    silent_ticks = 0
    speaking     = False

    while True:
        raw   = proc.stdout.read(VAD_CHUNK_BYTES)
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
                    # Natural end of speech — send burst
                    _burst_q.put(np.concatenate(buf))
                    buf = []
                    speaking     = False
                    silent_ticks = 0
            else:
                silent_ticks = 0
                if len(buf) >= VAD_MAX_CHUNKS:
                    # Burst too long — flush so Whisper stays current
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

    state     = "idle"
    question  = ""
    prev_text = ""
    perked    = False

    try:
        while True:
            burst = _burst_q.get()

            if state == "idle":
                # Phase 1 — tiny model, only fires on real speech bursts
                text = transcribe(burst, fast=True)
                if not text:
                    continue
                print(f"[mic] {text}")
                window = (prev_text + " " + text).strip()

                if has_wake(window) and not perked:
                    creature_cmd("perk")
                    perked = True
                    print("[Tony] Perking — heard name")

                if has_wake(window) and has_question_word(window):
                    state     = "recording"
                    after     = window.split("question", 1)[-1].strip(" .,!?") if "question" in window else ""
                    question  = after
                    prev_text = ""
                    perked    = False
                    print("[Tony] Recording question…")
                    creature_cmd("loading")
                    discord_post("🎙️ **Student is asking a question…**")
                else:
                    prev_text = text

            elif state == "recording":
                # Phase 2 — base model for accuracy
                text = transcribe(burst, fast=False)
                if not text:
                    continue
                print(f"[mic] {text}")
                clean = text.strip(" .,!?")

                if STOP_PHRASE in clean:
                    question += " " + clean.split(STOP_PHRASE)[0].strip(" .,!?")
                    question  = question.strip()
                    state     = "idle"
                    prev_text = ""
                    perked    = False
                    creature_cmd("responding")

                    if question:
                        print(f"[Tony] Question: {question}")
                        discord_post(f"❓ **Student asked:**\n> {question}")
                        answer = ask_groq(question)
                        print(f"[Tony] Answer: {answer}")
                        discord_post(f"💡 **Tony:**\n{answer}")

                    time.sleep(4)
                    creature_cmd("idle")
                    question = ""
                else:
                    question += " " + clean

    finally:
        proc.terminate()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Tony] Stopped.")
