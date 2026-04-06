# Prompt Log — Tony the Bot

---

## Session: 2026-04-02

---

### [1]

**User:**
in this project folder create a prompt log file with all of prompts and prompt responces

**Assistant:**
Created this `prompt_log.md` file in the project folder to log all prompts and responses from conversations in this project.

---

### [2]

**User:**
Evrytime i start a new prompt automatically log the prompt in th prompt_log.md file

**Assistant:**
Created `.claude/settings.json` with a `UserPromptSubmit` hook that automatically appends every new prompt to `prompt_log.md` with a timestamp.

---

### [3]

**User:**
end session

**Assistant:**
Session ended. Goodbye!

---

## Session: 2026-04-06

---

### [4]

**User:**
connect this files to tony1@TonysBrain rasberry pi and save a copy of the files in there

**Assistant:**
Attempted to connect via hostname `TonysBrain` — DNS resolution failed. Asked user for the Pi's IP address.

---

### [5]

**User:**
connect this files to tony1@10.1.65.108 rasberry pi and save a copy of the files in there

**Assistant:**
Installed `sshpass`, connected to the Pi at `10.1.65.108`, created `~/Tony_the_Bot/` directory, and copied `README.md` and `prompt_log.md` via rsync.

---

### [6]

**User:**
save all the previous promps and future prompts to the prompt_log.md file

**Assistant:**
Added all previous session prompts to `prompt_log.md` and fixed the `UserPromptSubmit` hook path so future prompts are automatically logged.

---

---

**[2026-04-06 19:23:26] User:**
for context tony1 is a Hexapod robot, lets start by connecting to the rasberry pi camera

---

**[2026-04-06 19:26:40] User:**
lets start by doing a live camera to my mac

---

**[2026-04-06 19:34:21] User:**
<task-notification>
