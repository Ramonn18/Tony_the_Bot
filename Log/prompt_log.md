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

### [7]

**User:**
for context tony1 is a Hexapod robot, lets start by connecting to the rasberry pi camera

**Assistant:**
Saved context that Tony is a Hexapod robot. Detected OV5647 camera (Pi Camera v1, 5MP) connected via CSI on the Raspberry Pi 5. Confirmed `picamera2` is available. Created `camera.py` with `connect_camera()`, `capture_frame()`, and `disconnect_camera()` functions. Tested successfully — captured a 640x480 RGB frame and saved a snapshot.

---

### [8]

**User:**
lets start by doing a live camera to my mac

**Assistant:**
Installed `opencv-python-headless` on the Pi. Created `stream_server.py` — a Flask MJPEG stream server running on port 8000. Stream is viewable in any browser at `http://10.1.65.108:8000`.

---

### [9]

**User:**
the camera feed look wrong, can you set it to the most optimal settings

**Assistant:**
Updated `stream_server.py` with optimized settings: resolution upgraded to 1280x720 HD, color format changed to BGR888 (fixes color issues — no conversion needed), JPEG quality raised to 85, auto white balance and auto exposure enabled, sharpness 1.5, saturation 1.2, contrast 1.1, and 2s warmup for AWB/AE to settle. Restarted the stream server on the Pi.

---

### [10]

**User:**
make sure to update the prompt_log.md with all pervious prompts and prompt responces

**Assistant:**
Cleaned up the prompt log — removed captured task-notifications, added all missing assistant responses, and reformatted all entries consistently.

---

### [11]

**User:**
connect to camera.py

**Assistant:**
Stopped the stream server to free the camera, then ran `camera.py` on the Pi. Camera connected successfully — OV5647 initialized, 640x480 RGB frame captured, snapshot saved to `~/Tony_the_Bot/snapshot.jpg`.

---

### [12]

**User:**
yes

**Assistant:**
Downloaded `snapshot.jpg` from the Pi to the Mac.

---

### [13]

**User:**
start a live feed of tony1 camera

**Assistant:**
Started `stream_server.py` on the Pi. Live feed available at `http://10.1.65.108:8000`.

---

### [14]

**User:**
do a small rasberry pi optimiced tiny yolo and show me the result on the live stream

**Assistant:**
Installed `ultralytics` (YOLOv8) on the Pi. Updated `stream_server.py` to run YOLOv8n inference at `imgsz=320` on each frame with bounding boxes and confidence labels drawn in real time. Resolution set to 640x480 for speed. Stream restarted at `http://10.1.65.108:8000`.

---

### [15]

**User:**
keep the install going

**Assistant:**
Waited for ultralytics install to complete. Successfully installed YOLOv8n + PyTorch on the Pi.

---

### [16]

**User:**
start live stream

**Assistant:**
Started `stream_server.py` with YOLOv8n detection. Live feed at `http://10.1.65.108:8000`.

---

### [17]

**User:**
turn off camera

**Assistant:**
Killed `stream_server.py` on the Pi. Camera off.

---

### [18]

**User:**
log every promp and prompt responce in the promtp_log.md file

**Assistant:**
Added all missing prompts and assistant responses from this session (entries 11–18) to `prompt_log.md` and synced to the Pi.

---

### [19]

**User:**
end session

**Assistant:**
Session ended. Goodbye!
