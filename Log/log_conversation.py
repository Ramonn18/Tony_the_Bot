#!/usr/bin/env python3
"""
log_conversation.py
Reads the current session transcript and appends the last assistant
response to prompt_log.md. Called by the Stop hook in settings.json.
"""
import sys
import json
import os
import glob
from datetime import datetime

LOG_FILE = "/Users/ramonnaula/Desktop/Classroom_Bot/Tony_the_Bot/Log/prompt_log.md"
PROJECT_DIR = os.path.expanduser(
    "~/.claude/projects/-Users-ramonnaula-Desktop-Classroom-Bot-Tony-the-Bot"
)


def get_session_id():
    try:
        data = json.loads(sys.stdin.read())
        return data.get("session_id") or data.get("sessionId")
    except Exception:
        return None


def find_transcript(session_id):
    if session_id:
        path = os.path.join(PROJECT_DIR, f"{session_id}.jsonl")
        if os.path.exists(path):
            return path
    # Fallback: most recently modified transcript
    files = glob.glob(os.path.join(PROJECT_DIR, "*.jsonl"))
    if files:
        return max(files, key=os.path.getmtime)
    return None


def extract_last_assistant_text(transcript_path):
    """Return the last assistant text message from the transcript."""
    last_text = None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if msg.get("role") == "assistant":
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            parts = [
                                c["text"]
                                for c in content
                                if c.get("type") == "text" and c.get("text", "").strip()
                            ]
                            if parts:
                                last_text = " ".join(parts)
                        elif isinstance(content, str) and content.strip():
                            last_text = content
                except Exception:
                    continue
    except Exception:
        pass
    return last_text


def append_response(text):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n**[{timestamp}] Assistant:**\n{text}\n\n---\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


def main():
    session_id = get_session_id()
    transcript = find_transcript(session_id)
    if not transcript:
        return
    text = extract_last_assistant_text(transcript)
    if text:
        append_response(text)


if __name__ == "__main__":
    main()
