from flask import Flask, Response, jsonify
from picamera2 import Picamera2
from ultralytics import YOLO
import libcamera
import cv2
import threading
import time
import json
import subprocess
from collections import deque
from datetime import datetime

# INA219 battery monitor — optional, graceful fallback if not connected
try:
    from ina219 import INA219, DeviceRangeError
    INA219_AVAILABLE = True
    SHUNT_OHMS = 0.1
    # 4x 18650 in series: 16.8V full, 12.0V empty
    BATTERY_MAX_V = 16.8
    BATTERY_MIN_V = 12.0
except ImportError:
    INA219_AVAILABLE = False

app = Flask(__name__)

cam = None
model = None
cam_lock = threading.Lock()

# Raw frame from camera (RGB) — written by camera thread, read by stream + YOLO
raw_frame = None
raw_frame_lock = threading.Lock()

# Last known bounding boxes from YOLO — overlaid on every streamed frame
last_boxes = []
boxes_lock = threading.Lock()

# Encoded JPEG served to browsers
latest_frame = None
frame_lock = threading.Lock()

# FPS counters
fps_camera = 0.0
fps_stream = 0.0
fps_yolo   = 0.0

# Detection state
latest_detections = []
detection_history = deque(maxlen=100)
detections_lock = threading.Lock()
sse_clients = []
sse_lock = threading.Lock()


def start_camera():
    global cam
    cam = Picamera2()
    config = cam.create_video_configuration(
        main={"size": (640, 480), "format": "RGB888"},
        controls={
            "FrameRate": 30,
            "AwbEnable": True,
            "AwbMode": libcamera.controls.AwbModeEnum.Indoor,
            "AeEnable": True,
            "AeMeteringMode": libcamera.controls.AeMeteringModeEnum.Matrix,
            "AeExposureMode": libcamera.controls.AeExposureModeEnum.Normal,
            "Sharpness": 1.0,
            "Contrast": 1.0,
            "Saturation": 1.0,
            "Brightness": 0.0,
            "NoiseReductionMode": libcamera.controls.draft.NoiseReductionModeEnum.Fast,
        },
        buffer_count=4,
    )
    cam.configure(config)
    cam.start()
    time.sleep(4)  # allow AWB to fully converge
    print("Camera started — 640x480 @ 30fps")


def load_model():
    global model
    model = YOLO("yolov8n.pt")
    model.fuse()
    print("YOLOv8n loaded and fused.")


def draw_boxes(frame, boxes):
    """Draw pre-computed boxes [(x1,y1,x2,y2,label), ...] onto a BGR frame."""
    for (x1, y1, x2, y2, label) in boxes:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 255, 0), -1)
        cv2.putText(frame, label, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    return frame


def push_sse(data):
    """Push a JSON event to all connected SSE clients."""
    msg = f"data: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.append(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def camera_loop():
    """Captures frames from the camera at full speed into raw_frame."""
    global raw_frame, fps_camera
    count, t0 = 0, time.time()
    while True:
        try:
            with cam_lock:
                frame = cam.capture_array()
            with raw_frame_lock:
                raw_frame = frame
            count += 1
            elapsed = time.time() - t0
            if elapsed >= 2.0:
                fps_camera = round(count / elapsed, 1)
                count, t0 = 0, time.time()
        except Exception as e:
            print(f"[camera_loop error] {e}", flush=True)
            time.sleep(0.1)


def detection_loop():
    """Runs YOLO as fast as CPU allows — only updates boxes, never blocks the stream."""
    global latest_detections, fps_yolo
    last_labels = set()
    count, t0 = 0, time.time()

    while True:
        try:
            with raw_frame_lock:
                frame = raw_frame
            if frame is None:
                time.sleep(0.01)
                continue

            results = model.predict(frame, imgsz=320, conf=0.4, verbose=False, device="cpu")

            # Extract boxes as plain tuples for draw_boxes()
            boxes = []
            detections = []
            for result in results:
                for box in result.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    conf = float(box.conf[0])
                    label = f"{model.names[int(box.cls[0])]} {conf:.0%}"
                    boxes.append((x1, y1, x2, y2, label))
                    detections.append({
                        "label": model.names[int(box.cls[0])],
                        "confidence": round(conf, 2),
                    })

            with boxes_lock:
                last_boxes[:] = boxes
            with detections_lock:
                latest_detections = detections
            count += 1
            elapsed = time.time() - t0
            if elapsed >= 2.0:
                fps_yolo = round(count / elapsed, 1)
                count, t0 = 0, time.time()

            current_labels = {d["label"] for d in detections}
            if current_labels != last_labels:
                event = {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "objects": detections,
                }
                with detections_lock:
                    detection_history.appendleft(event)
                push_sse(event)
                last_labels = current_labels

        except Exception as e:
            print(f"[detection_loop error] {e}", flush=True)
            time.sleep(0.1)


def stream_loop():
    """Encodes camera frames at full speed with latest boxes overlaid."""
    global latest_frame, fps_stream
    count, t0 = 0, time.time()
    while True:
        try:
            with raw_frame_lock:
                frame = raw_frame
            if frame is None:
                time.sleep(0.01)
                continue

            bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            with boxes_lock:
                boxes = list(last_boxes)
            bgr = draw_boxes(bgr, boxes)

            _, jpeg = cv2.imencode(
                ".jpg", bgr,
                [cv2.IMWRITE_JPEG_QUALITY, 70, cv2.IMWRITE_JPEG_OPTIMIZE, 1]
            )
            with frame_lock:
                latest_frame = jpeg.tobytes()

            count += 1
            elapsed = time.time() - t0
            if elapsed >= 2.0:
                fps_stream = round(count / elapsed, 1)
                count, t0 = 0, time.time()
            time.sleep(0.0417)  # cap at ~24fps

        except Exception as e:
            print(f"[stream_loop error] {e}", flush=True)
            time.sleep(0.1)


def generate():
    last = None
    while True:
        with frame_lock:
            frame = latest_frame
        if frame is not None and frame is not last:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
            last = frame
        time.sleep(0.0417)  # match 24fps — never spin faster than stream_loop


@app.route("/video_feed")
def video_feed():
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/detections")
def detections():
    with detections_lock:
        return jsonify({
            "current": latest_detections,
            "history": list(detection_history),
        })


def get_system_stats():
    """Read CPU %, RAM %, and CPU temperature."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory()
        ram_pct  = ram.percent
        ram_used = round(ram.used / 1024 / 1024)
        ram_total = round(ram.total / 1024 / 1024)
    except Exception:
        cpu, ram_pct, ram_used, ram_total = None, None, None, None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp_c = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        temp_c = None
    return {
        "cpu_pct":   cpu,
        "ram_pct":   ram_pct,
        "ram_used":  ram_used,
        "ram_total": ram_total,
        "temp_c":    temp_c,
    }


def get_signal_strength():
    """Read WiFi signal from /proc/net/wireless. Returns dBm and quality 0-100."""
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if not parts:
                continue
            dbm = float(parts[3].strip("."))
            if dbm > 0:
                dbm -= 256  # unsigned to signed conversion
            # Map dBm to 0-100%: -30=100%, -90=0%
            quality = max(0, min(100, int((dbm + 90) / 60 * 100)))
            return {"dbm": int(dbm), "quality": quality}
    except Exception:
        return {"dbm": None, "quality": None}


@app.route("/snapshot")
def snapshot():
    with frame_lock:
        frame = latest_frame
    if frame is None:
        return "No frame available", 503
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        frame,
        mimetype="image/jpeg",
        headers={"Content-Disposition": f"attachment; filename=tony_{timestamp}.jpg"}
    )


@app.route("/stats")
def stats():
    signal = get_signal_strength()
    system = get_system_stats()
    return jsonify({
        "fps_camera":    fps_camera,
        "fps_stream":    fps_stream,
        "fps_yolo":      fps_yolo,
        "signal_dbm":    signal["dbm"],
        "signal_quality":signal["quality"],
        **system,
    })


@app.route("/power")
def power():
    result = {
        "throttled": False,
        "under_voltage": False,
        "throttle_raw": None,
        "voltage": None,
        "current_ma": None,
        "battery_pct": None,
        "source": "unknown",
    }

    # --- vcgencmd throttle / under-voltage check ---
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True).strip()
        # e.g. "throttled=0x0" or "throttled=0x50000"
        hex_val = int(out.split("=")[1], 16)
        result["throttle_raw"] = hex(hex_val)
        result["under_voltage"] = bool(hex_val & 0x1)        # bit 0: currently under-voltage
        result["throttled"] = bool(hex_val & 0x4)            # bit 2: currently throttled
        result["source"] = "wall"
    except Exception:
        pass

    # --- INA219 battery reading (when hardware is connected) ---
    if INA219_AVAILABLE:
        try:
            ina = INA219(SHUNT_OHMS)
            ina.configure()
            voltage = ina.voltage()
            try:
                current = ina.current()
            except DeviceRangeError:
                current = None
            pct = max(0.0, min(100.0, (voltage - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V) * 100))
            result["voltage"] = round(voltage, 2)
            result["current_ma"] = round(current, 1) if current is not None else None
            result["battery_pct"] = round(pct, 1)
            result["source"] = "charging" if (current and current > 50) else "battery"
        except Exception:
            pass

    return jsonify(result)


@app.route("/events")
def events():
    """Server-Sent Events stream — pushes detection changes in real time."""
    client_queue = deque(maxlen=20)
    with sse_lock:
        sse_clients.append(client_queue)

    def stream():
        try:
            while True:
                if client_queue:
                    yield client_queue.popleft()
                else:
                    yield ": ping\n\n"
                    time.sleep(1)
        except GeneratorExit:
            with sse_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>// TONY_MONITOR</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --yellow:  #f5c518;
      --cyan:    #00d4ff;
      --magenta: #ff003c;
      --orange:  #ff8c00;
      --bg:      #080810;
      --panel:   #0d0d1a;
      --panel2:  #0a0a16;
      --border:  #1a1a33;
      --border2: #22223a;
      --text:    #c8c8d8;
      --dim:     #44445a;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Share Tech Mono', monospace;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    /* ── SCANLINE OVERLAY ── */
    body::after {
      content: '';
      position: fixed; inset: 0;
      background: repeating-linear-gradient(
        to bottom,
        transparent 0px,
        transparent 3px,
        rgba(0,0,0,0.08) 3px,
        rgba(0,0,0,0.08) 4px
      );
      pointer-events: none;
      z-index: 9999;
    }

    /* ── HEADER ── */
    header {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 0 20px;
      height: 52px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      position: relative;
    }
    /* yellow left accent bar */
    header::before {
      content: '';
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 3px;
      background: var(--yellow);
    }

    .brand {
      display: flex; flex-direction: column; line-height: 1.1;
    }
    .brand-title {
      font-family: 'Rajdhani', sans-serif;
      font-size: 1.1rem; font-weight: 700;
      color: var(--yellow);
      letter-spacing: 3px;
      text-transform: uppercase;
    }
    .brand-sub {
      font-size: 0.55rem;
      color: var(--dim);
      letter-spacing: 2px;
    }

    #status-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--dim); flex-shrink: 0;
      transition: background 0.3s, box-shadow 0.3s;
    }
    #status-dot.live {
      background: var(--cyan);
      box-shadow: 0 0 8px var(--cyan);
    }

    .counts { display: flex; gap: 8px; flex-wrap: wrap; }
    .count-badge {
      background: rgba(0,212,255,0.06);
      border: 1px solid rgba(0,212,255,0.2);
      clip-path: polygon(6px 0%, 100% 0%, calc(100% - 6px) 100%, 0% 100%);
      padding: 2px 12px;
      font-size: 0.68rem;
      color: var(--dim);
      letter-spacing: 1px;
    }
    .count-badge span { color: var(--cyan); }

    /* header chip shared style */
    .hchip {
      display: flex; align-items: center; gap: 8px;
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--border2);
      padding: 4px 12px;
      font-size: 0.68rem;
      flex-shrink: 0;
    }
    .hlabel { color: var(--dim); letter-spacing: 1px; text-transform: uppercase; }
    .hval   { color: var(--text); font-weight: bold; }
    .hval.ok   { color: var(--cyan); }
    .hval.warn { color: var(--orange); }
    .hval.crit { color: var(--magenta); animation: blink 0.8s step-start infinite; }

    /* snapshot button */
    .snap-btn {
      background: transparent;
      border: 1px solid var(--yellow);
      clip-path: polygon(8px 0%, 100% 0%, calc(100% - 8px) 100%, 0% 100%);
      padding: 4px 18px;
      color: var(--yellow);
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.68rem; letter-spacing: 2px;
      cursor: pointer; text-transform: uppercase; flex-shrink: 0;
      transition: background 0.15s;
    }
    .snap-btn:hover  { background: rgba(245,197,24,0.12); }
    .snap-btn.flash  { background: var(--yellow); color: #000; }

    /* signal bars */
    .signal-bars {
      display: flex; align-items: flex-end; gap: 2px; height: 14px;
    }
    .signal-bar { width: 4px; background: var(--border2); transition: background 0.4s; }
    .signal-bar.b1 { height: 4px; }
    .signal-bar.b2 { height: 7px; }
    .signal-bar.b3 { height: 10px; }
    .signal-bar.b4 { height: 14px; }
    .signal-bar.active      { background: var(--cyan); }
    .signal-bar.active.warn { background: var(--orange); }
    .signal-bar.active.crit { background: var(--magenta); }
    .signal-dbm { color: var(--dim); font-size: 0.6rem; }

    /* battery bar */
    .battery-bar-wrap {
      width: 52px; height: 6px;
      background: var(--border); overflow: hidden;
      border: 1px solid var(--border2);
    }
    .battery-bar {
      height: 100%;
      transition: width 0.5s ease, background 0.5s ease;
    }

    /* push power panel to right */
    #power-panel { margin-left: auto; }

    /* ── MAIN LAYOUT ── */
    .main { display: flex; flex: 1; overflow: hidden; }

    /* ── VIDEO PANEL ── */
    .video-panel {
      flex: 1;
      display: flex; align-items: center; justify-content: center;
      padding: 16px;
      background: var(--bg);
      position: relative;
    }
    .video-wrap {
      position: relative;
      display: inline-block;
      line-height: 0;
    }
    .video-wrap img {
      max-width: 100%; max-height: calc(100vh - 120px);
      border: 1px solid var(--border);
      display: block;
    }
    /* HUD corner brackets */
    .corner {
      position: absolute; width: 18px; height: 18px;
      border-color: var(--yellow); border-style: solid;
    }
    .corner.tl { top: -1px; left: -1px;  border-width: 2px 0 0 2px; }
    .corner.tr { top: -1px; right: -1px; border-width: 2px 2px 0 0; }
    .corner.bl { bottom: -1px; left: -1px;  border-width: 0 0 2px 2px; }
    .corner.br { bottom: -1px; right: -1px; border-width: 0 2px 2px 0; }

    /* ── SIDEBAR ── */
    .sidebar {
      width: 290px;
      border-left: 1px solid var(--border);
      display: flex; flex-direction: column;
      background: var(--panel2);
      flex-shrink: 0;
    }

    .sec-header {
      padding: 8px 14px;
      font-size: 0.65rem; color: var(--dim);
      letter-spacing: 2px; text-transform: uppercase;
      border-bottom: 1px solid var(--border);
      display: flex; align-items: center; gap: 6px;
    }
    .sec-header::before {
      content: '◆';
      color: var(--yellow); font-size: 0.5rem;
    }

    #event-log {
      flex: 1; overflow-y: auto; padding: 6px 0;
      scrollbar-width: thin;
      scrollbar-color: var(--border) transparent;
    }
    .event {
      padding: 7px 14px;
      border-bottom: 1px solid var(--border);
      animation: fadeIn 0.25s ease;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; } }
    .event-time { font-size: 0.6rem; color: var(--dim); margin-bottom: 4px; }
    .event-empty { font-size: 0.7rem; color: var(--border2); font-style: italic; }

    .tag {
      display: inline-block; margin: 2px 3px 2px 0;
      padding: 1px 8px;
      font-size: 0.67rem; font-weight: bold;
      clip-path: polygon(4px 0%, 100% 0%, calc(100% - 4px) 100%, 0% 100%);
    }
    .tag-person { background: rgba(255,0,60,0.15);  color: #ff6688; border: 1px solid rgba(255,0,60,0.4); }
    .tag-object { background: rgba(0,212,255,0.12); color: var(--cyan); border: 1px solid rgba(0,212,255,0.3); }

    /* ── SYSTEM STATS ── */
    .sys-panel {
      border-top: 1px solid var(--border);
      padding: 10px 14px;
      display: flex; flex-direction: column; gap: 8px;
      flex-shrink: 0;
    }
    .stat-row { display: flex; align-items: center; gap: 8px; }
    .stat-label {
      font-size: 0.6rem; color: var(--dim);
      text-transform: uppercase; width: 34px; flex-shrink: 0;
      letter-spacing: 1px;
    }
    .stat-bar-wrap {
      flex: 1; height: 4px; background: var(--border); overflow: hidden;
    }
    .stat-bar {
      height: 100%;
      transition: width 0.5s ease, background 0.5s ease;
    }
    .stat-value {
      font-size: 0.65rem; text-align: right;
      width: 56px; flex-shrink: 0;
    }
    .stat-value.ok   { color: var(--cyan); }
    .stat-value.warn { color: var(--orange); }
    .stat-value.crit { color: var(--magenta); }

    .temp-warn {
      font-size: 0.62rem; color: var(--magenta);
      text-align: center; letter-spacing: 2px;
      animation: blink 0.8s step-start infinite;
    }
    @keyframes blink { 50% { opacity: 0; } }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="brand-title">// TONY_MONITOR</span>
      <span class="brand-sub">NEURAL SURVEILLANCE SYSTEM v2.0</span>
    </div>
    <div id="status-dot"></div>

    <div class="counts" id="counts"></div>

    <button class="snap-btn" id="snap-btn" onclick="takeSnapshot()">&#9654; SNAPSHOT</button>

    <!-- WiFi signal -->
    <div class="hchip">
      <div class="signal-bars">
        <div class="signal-bar b1" id="sig1"></div>
        <div class="signal-bar b2" id="sig2"></div>
        <div class="signal-bar b3" id="sig3"></div>
        <div class="signal-bar b4" id="sig4"></div>
      </div>
      <span class="hlabel">WIFI</span>
      <span class="signal-dbm" id="signal-dbm">-- dBm</span>
    </div>

    <!-- FPS chip -->
    <div class="hchip">
      <span class="hlabel">CAM</span>
      <span class="hval ok" id="fps-cam">--</span>
      <span class="hlabel">fps</span>
      &nbsp;
      <span class="hlabel">STR</span>
      <span class="hval ok" id="fps-stream">--</span>
      <span class="hlabel">fps</span>
      &nbsp;
      <span class="hlabel">AI</span>
      <span class="hval" id="fps-yolo">--</span>
      <span class="hlabel">fps</span>
    </div>

    <!-- Power chip -->
    <div class="hchip" id="power-panel">
      <span class="hlabel">PWR</span>
      <span class="hval" id="power-status">--</span>
      <div id="battery-bar-wrap" class="battery-bar-wrap" style="display:none">
        <div id="battery-bar" class="battery-bar"></div>
      </div>
      <span class="hval" id="battery-pct" style="display:none"></span>
      <span class="hval" style="color:var(--dim)" id="battery-v" style="display:none"></span>
    </div>
  </header>

  <div class="main">
    <div class="video-panel">
      <div class="video-wrap">
        <div class="corner tl"></div>
        <div class="corner tr"></div>
        <div class="corner bl"></div>
        <div class="corner br"></div>
        <img src="/video_feed" alt="Live feed">
      </div>
    </div>

    <div class="sidebar">
      <div class="sec-header">Detection Events</div>
      <div id="event-log"></div>

      <div class="sys-panel">
        <div class="sec-header" style="padding:0 0 4px 0; border:none">System</div>
        <div class="stat-row">
          <span class="stat-label">CPU</span>
          <div class="stat-bar-wrap"><div class="stat-bar" id="cpu-bar"></div></div>
          <span class="stat-value" id="cpu-val">--%</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">RAM</span>
          <div class="stat-bar-wrap"><div class="stat-bar" id="ram-bar"></div></div>
          <span class="stat-value" id="ram-val">--%</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">TEMP</span>
          <div class="stat-bar-wrap"><div class="stat-bar" id="temp-bar"></div></div>
          <span class="stat-value" id="temp-val">-- °C</span>
        </div>
        <div id="temp-warn" class="temp-warn" style="display:none">⚠ THERMAL ALERT</div>
      </div>
    </div>
  </div>

  <script>
    const log = document.getElementById('event-log');
    const countsEl = document.getElementById('counts');
    const dot = document.getElementById('status-dot');
    const counts = {};

    function tagHTML(obj) {
      const isPerson = obj.label === 'person';
      const cls = isPerson ? 'tag-person' : 'tag-object';
      return `<span class="tag ${cls}">${obj.label} ${Math.round(obj.confidence * 100)}%</span>`;
    }

    function updateCounts(objects) {
      objects.forEach(o => { counts[o.label] = (counts[o.label] || 0) + 1; });
      countsEl.innerHTML = Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6)
        .map(([k, v]) => `<div class="count-badge">${k} <span>${v}</span></div>`)
        .join('');
    }

    function addEvent(ev) {
      const div = document.createElement('div');
      div.className = 'event';
      if (ev.objects.length === 0) {
        div.innerHTML = `<div class="event-time">${ev.timestamp}</div>
                         <div class="event-empty">// null detection</div>`;
      } else {
        div.innerHTML = `<div class="event-time">${ev.timestamp}</div>
                         ${ev.objects.map(tagHTML).join('')}`;
        updateCounts(ev.objects);
      }
      log.prepend(div);
      while (log.children.length > 80) log.removeChild(log.lastChild);
    }

    fetch('/detections').then(r => r.json()).then(data => {
      data.history.slice().reverse().forEach(addEvent);
      dot.classList.add('live');
    });

    const es = new EventSource('/events');
    es.onmessage = e => { addEvent(JSON.parse(e.data)); };
    es.onerror = () => dot.classList.remove('live');
    es.onopen  = () => dot.classList.add('live');

    function updatePower() {
      fetch('/power').then(r => r.json()).then(p => {
        const status = document.getElementById('power-status');
        const bar    = document.getElementById('battery-bar');
        const barWrap= document.getElementById('battery-bar-wrap');
        const pctEl  = document.getElementById('battery-pct');
        const vEl    = document.getElementById('battery-v');

        if (p.under_voltage || p.throttled) {
          status.textContent = p.throttled ? 'THROTTLED' : 'UV!';
          status.className = 'hval crit';
        } else if (p.source === 'wall' || p.source === 'unknown') {
          status.textContent = 'AC';
          status.className = 'hval ok';
        } else if (p.source === 'charging') {
          status.textContent = 'CHG';
          status.className = 'hval ok';
        } else {
          status.textContent = 'BATT';
          status.className = 'hval ok';
        }

        if (p.battery_pct !== null) {
          const pct = p.battery_pct;
          barWrap.style.display = 'block';
          pctEl.style.display   = 'inline';
          vEl.style.display     = 'inline';
          bar.style.width       = pct + '%';
          bar.style.background  = pct > 50 ? 'var(--cyan)' : pct > 20 ? 'var(--orange)' : 'var(--magenta)';
          pctEl.textContent     = pct.toFixed(0) + '%';
          pctEl.className       = 'hval ' + (pct > 50 ? 'ok' : pct > 20 ? 'warn' : 'crit');
          vEl.textContent       = p.voltage + 'V';
          if (pct <= 10) { status.textContent = 'LOW'; status.className = 'hval crit'; }
        } else {
          barWrap.style.display = 'none';
          pctEl.style.display   = 'none';
          vEl.style.display     = 'none';
        }
      }).catch(() => {
        document.getElementById('power-status').textContent = 'ERR';
      });
    }
    updatePower();
    setInterval(updatePower, 6000);

    function setBar(barId, valId, pct, value, threshWarn, threshCrit) {
      const bar = document.getElementById(barId);
      const val = document.getElementById(valId);
      const cls   = pct >= threshCrit ? 'crit' : pct >= threshWarn ? 'warn' : 'ok';
      const color = pct >= threshCrit ? 'var(--magenta)' : pct >= threshWarn ? 'var(--orange)' : 'var(--cyan)';
      bar.style.width      = pct + '%';
      bar.style.background = color;
      val.textContent      = value;
      val.className        = 'stat-value ' + cls;
    }

    function updateStats() {
      fetch('/stats').then(r => r.json()).then(s => {
        document.getElementById('fps-cam').textContent    = s.fps_camera;
        document.getElementById('fps-stream').textContent = s.fps_stream;
        const yoloEl = document.getElementById('fps-yolo');
        yoloEl.textContent = s.fps_yolo;
        yoloEl.className   = 'hval ' + (s.fps_yolo >= 3 ? 'ok' : 'warn');

        if (s.cpu_pct !== null) setBar('cpu-bar', 'cpu-val', s.cpu_pct, s.cpu_pct.toFixed(0) + '%', 70, 90);
        if (s.ram_pct !== null) setBar('ram-bar', 'ram-val', s.ram_pct, s.ram_used + '/' + s.ram_total + 'MB', 75, 90);
        if (s.temp_c  !== null) {
          const tempPct = Math.min(100, ((s.temp_c - 30) / 60) * 100);
          setBar('temp-bar', 'temp-val', tempPct, s.temp_c + ' °C', 60, 75);
          document.getElementById('temp-warn').style.display = s.temp_c >= 80 ? 'block' : 'none';
        }

        const q = s.signal_quality, dbm = s.signal_dbm;
        if (q !== null) {
          const bars = Math.ceil(q / 25);
          const cls  = q >= 60 ? 'active' : q >= 35 ? 'active warn' : 'active crit';
          for (let i = 1; i <= 4; i++) {
            const el = document.getElementById('sig' + i);
            el.className = 'signal-bar b' + i + (i <= bars ? ' ' + cls : '');
          }
          document.getElementById('signal-dbm').textContent = dbm + ' dBm';
        }
      });
    }
    updateStats();
    setInterval(updateStats, 2000);

    function takeSnapshot() {
      const btn = document.getElementById('snap-btn');
      btn.classList.add('flash');
      btn.textContent = '✓ CAPTURED';
      const a = document.createElement('a');
      a.href = '/snapshot'; a.download = ''; a.click();
      setTimeout(() => {
        btn.classList.remove('flash');
        btn.innerHTML = '&#9654; SNAPSHOT';
      }, 1200);
    }
  </script>
</body>
</html>"""


if __name__ == "__main__":
    load_model()
    start_camera()
    threading.Thread(target=camera_loop, daemon=True).start()
    threading.Thread(target=detection_loop, daemon=True).start()
    threading.Thread(target=stream_loop, daemon=True).start()
    print("Stream live at http://0.0.0.0:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True)
