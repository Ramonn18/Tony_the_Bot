#!/usr/bin/env python3
"""
Stop hook script: reads the current session's JSONL transcript and appends
new user prompts + assistant responses to conversation_log.md.
"""
import json
import sys
from pathlib import Path
from datetime import datetime


def extract_text(content):
    """Pull plain text out of a message content field (str or list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts).strip()
    return ""


def is_tool_result_only(content):
    """Return True if the content list contains only tool_result items."""
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(item, dict) and item.get("type") == "tool_result"
        for item in content
    )


def main():
    try:
        data = json.load(sys.stdin)
        session_id = data.get("session_id", "")
    except Exception:
        session_id = ""

    if not session_id:
        return

    # Find the JSONL file across all project dirs
    projects_root = Path.home() / ".claude" / "projects"
    jsonl_path = None
    for project_dir in projects_root.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            jsonl_path = candidate
            break

    if jsonl_path is None:
        return

    log_path = Path("/Users/ramonnaula/Desktop/Classroom_Bot/Tony_the_Bot/Log/conversation_log.md")
    state_path = Path("/Users/ramonnaula/Desktop/Classroom_Bot/Tony_the_Bot/Log/.log_state.json")

    # Load state (tracks how many lines we've already processed per session)
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            state = {}

    last_line_idx = state.get(session_id, 0)

    # Read JSONL
    raw_lines = jsonl_path.read_text().splitlines()
    new_lines = raw_lines[last_line_idx:]

    # Parse new entries into (role, text, timestamp) tuples
    exchanges = []
    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        entry_type = entry.get("type")
        msg = entry.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", "")
        ts = entry.get("timestamp", "")[:19].replace("T", " ")

        if entry_type == "user" and role == "user":
            if is_tool_result_only(content):
                continue
            text = extract_text(content)
            # Skip internal system messages and skill expansions
            # (skill expansions are very long messages starting with a markdown H1)
            if not text:
                continue
            if text.startswith("<"):
                continue
            if text.startswith("# ") and len(text) > 500:
                continue
            exchanges.append(("user", text, ts))

        elif entry_type == "assistant" and role == "assistant":
            text = extract_text(content)
            if text:
                exchanges.append(("assistant", text, ts))

    # Group exchanges: pair each user prompt with the LAST assistant response after it
    pairs = []
    i = 0
    while i < len(exchanges):
        role, text, ts = exchanges[i]
        if role == "user":
            user_text, user_ts = text, ts
            last_assistant = None
            i += 1
            while i < len(exchanges) and exchanges[i][0] == "assistant":
                last_assistant = (exchanges[i][1], exchanges[i][2])
                i += 1
            pairs.append((user_text, user_ts, last_assistant))
        else:
            i += 1

    # Write to log
    if pairs:
        if not log_path.exists():
            log_path.write_text("# Conversation Log — Tony the Bot\n\n")

        with open(log_path, "a") as f:
            for user_text, user_ts, assistant in pairs:
                f.write(f"\n---\n\n**[{user_ts}] User:**\n{user_text}\n\n")
                if assistant:
                    f.write(f"**Assistant:**\n{assistant[0]}\n")

    # Update state
    state[session_id] = len(raw_lines)
    state_path.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
