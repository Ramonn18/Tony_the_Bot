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


@app.route("/stats")
def stats():
    return jsonify({
        "fps_camera": fps_camera,
        "fps_stream": fps_stream,
        "fps_yolo":   fps_yolo,
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
  <title>Tony the Bot — Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d0d0d;
      color: #e0e0e0;
      font-family: 'Courier New', monospace;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }

    header {
      padding: 12px 20px;
      background: #111;
      border-bottom: 1px solid #1a1a1a;
      display: flex;
      align-items: center;
      gap: 16px;
    }
    header h1 { font-size: 1rem; color: #00ff88; letter-spacing: 2px; }
    #status-dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: #444; flex-shrink: 0;
    }
    #status-dot.live { background: #00ff88; box-shadow: 0 0 6px #00ff88; }

    .counts {
      display: flex; gap: 12px; flex-wrap: wrap;
    }
    .count-badge {
      background: #1a1a1a;
      border: 1px solid #333;
      border-radius: 4px;
      padding: 2px 10px;
      font-size: 0.75rem;
      color: #aaa;
    }
    .count-badge span { color: #00ff88; font-weight: bold; }

    /* Power panel */
    .power-panel {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 10px;
      background: #161616;
      border: 1px solid #2a2a2a;
      border-radius: 6px;
      padding: 4px 12px;
      font-size: 0.72rem;
      flex-shrink: 0;
    }
    .power-icon { font-size: 1rem; }
    .power-label { color: #666; letter-spacing: 1px; text-transform: uppercase; }
    .power-value { color: #e0e0e0; font-weight: bold; }
    .power-value.ok    { color: #00ff88; }
    .power-value.warn  { color: #ffcc00; }
    .power-value.crit  { color: #ff4444; }

    .battery-bar-wrap {
      width: 60px; height: 8px;
      background: #222; border-radius: 4px; overflow: hidden;
      border: 1px solid #333;
    }
    .battery-bar {
      height: 100%; border-radius: 4px;
      transition: width 0.5s ease, background 0.5s ease;
    }

    .main {
      display: flex;
      flex: 1;
      overflow: hidden;
    }

    .video-panel {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
      background: #0d0d0d;
    }
    .video-panel img {
      max-width: 100%;
      max-height: 100%;
      border: 1px solid #222;
      border-radius: 4px;
    }

    .sidebar {
      width: 300px;
      border-left: 1px solid #1a1a1a;
      display: flex;
      flex-direction: column;
      background: #111;
    }
    .sidebar-header {
      padding: 10px 14px;
      font-size: 0.7rem;
      color: #555;
      letter-spacing: 1px;
      border-bottom: 1px solid #1a1a1a;
      text-transform: uppercase;
    }
    #event-log {
      flex: 1;
      overflow-y: auto;
      padding: 8px 0;
    }
    .event {
      padding: 8px 14px;
      border-bottom: 1px solid #161616;
      animation: fadeIn 0.3s ease;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; } }
    .event-time { font-size: 0.65rem; color: #444; margin-bottom: 4px; }
    .event-empty { font-size: 0.75rem; color: #333; font-style: italic; }
    .tag {
      display: inline-block;
      margin: 2px 3px 2px 0;
      padding: 1px 7px;
      border-radius: 3px;
      font-size: 0.7rem;
      font-weight: bold;
    }
    .tag-person { background: #1a0a2e; color: #cc88ff; border: 1px solid #440088; }
    .tag-object { background: #0a1a0a; color: #00cc66; border: 1px solid #005522; }
  </style>
</head>
<body>
  <header>
    <div id="status-dot"></div>
    <h1>TONY THE BOT &mdash; MONITOR</h1>
    <div class="counts" id="counts"></div>
    <div class="power-panel" style="margin-left:0">
      <span class="power-label">CAM</span>
      <span class="power-value ok" id="fps-cam">--</span>
      <span class="power-label">fps</span>
      &nbsp;
      <span class="power-label">STREAM</span>
      <span class="power-value ok" id="fps-stream">--</span>
      <span class="power-label">fps</span>
      &nbsp;
      <span class="power-label">YOLO</span>
      <span class="power-value" id="fps-yolo">--</span>
      <span class="power-label">fps</span>
    </div>
    <div class="power-panel" id="power-panel">
      <span class="power-icon" id="power-icon">⚡</span>
      <span class="power-label">POWER</span>
      <span class="power-value" id="power-status">--</span>
      <div id="battery-bar-wrap" class="battery-bar-wrap" style="display:none">
        <div id="battery-bar" class="battery-bar"></div>
      </div>
      <span class="power-value" id="battery-pct" style="display:none"></span>
      <span class="power-value" id="battery-v" style="display:none"></span>
    </div>
  </header>

  <div class="main">
    <div class="video-panel">
      <img src="/video_feed" alt="Live feed">
    </div>
    <div class="sidebar">
      <div class="sidebar-header">Detection Events</div>
      <div id="event-log"></div>
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
                         <div class="event-empty">nothing detected</div>`;
      } else {
        div.innerHTML = `<div class="event-time">${ev.timestamp}</div>
                         ${ev.objects.map(tagHTML).join('')}`;
        updateCounts(ev.objects);
      }
      log.prepend(div);

      // Keep log from growing too large in the DOM
      while (log.children.length > 80) log.removeChild(log.lastChild);
    }

    // Load history on page open
    fetch('/detections').then(r => r.json()).then(data => {
      data.history.slice().reverse().forEach(addEvent);
      dot.classList.add('live');
    });

    // SSE for real-time updates
    const es = new EventSource('/events');
    es.onmessage = e => { addEvent(JSON.parse(e.data)); };
    es.onerror = () => dot.classList.remove('live');
    es.onopen  = () => dot.classList.add('live');

    // Power panel — poll every 6 seconds
    function updatePower() {
      fetch('/power').then(r => r.json()).then(p => {
        const icon   = document.getElementById('power-icon');
        const status = document.getElementById('power-status');
        const bar    = document.getElementById('battery-bar');
        const barWrap= document.getElementById('battery-bar-wrap');
        const pctEl  = document.getElementById('battery-pct');
        const vEl    = document.getElementById('battery-v');

        // Under-voltage / throttle warning
        if (p.under_voltage || p.throttled) {
          icon.textContent = '⚠️';
          status.textContent = p.throttled ? 'THROTTLED' : 'UNDER-VOLTAGE';
          status.className = 'power-value crit';
        } else if (p.source === 'wall' || p.source === 'unknown') {
          icon.textContent = '🔌';
          status.textContent = 'PLUGGED IN';
          status.className = 'power-value ok';
        } else if (p.source === 'charging') {
          icon.textContent = '🔋';
          status.textContent = 'CHARGING';
          status.className = 'power-value ok';
        } else {
          icon.textContent = '🔋';
          status.textContent = 'BATTERY';
          status.className = 'power-value ok';
        }

        // Battery gauge (only shown when INA219 is connected)
        if (p.battery_pct !== null) {
          const pct = p.battery_pct;
          barWrap.style.display = 'block';
          pctEl.style.display   = 'inline';
          vEl.style.display     = 'inline';
          bar.style.width       = pct + '%';
          bar.style.background  = pct > 50 ? '#00ff88' : pct > 20 ? '#ffcc00' : '#ff4444';
          pctEl.textContent     = pct.toFixed(0) + '%';
          pctEl.className       = 'power-value ' + (pct > 50 ? 'ok' : pct > 20 ? 'warn' : 'crit');
          vEl.textContent       = p.voltage + 'V';
          vEl.className         = 'power-value';
          if (pct <= 10) {
            icon.textContent = '🪫';
            status.textContent = 'LOW BATTERY';
            status.className = 'power-value crit';
          }
        } else {
          barWrap.style.display = 'none';
          pctEl.style.display   = 'none';
          vEl.style.display     = 'none';
        }
      }).catch(() => {
        document.getElementById('power-status').textContent = 'N/A';
      });
    }

    updatePower();
    setInterval(updatePower, 6000);

    function updateStats() {
      fetch('/stats').then(r => r.json()).then(s => {
        document.getElementById('fps-cam').textContent    = s.fps_camera;
        document.getElementById('fps-stream').textContent = s.fps_stream;
        const yoloEl = document.getElementById('fps-yolo');
        yoloEl.textContent  = s.fps_yolo;
        yoloEl.className    = 'power-value ' + (s.fps_yolo >= 3 ? 'ok' : 'warn');
      });
    }
    updateStats();
    setInterval(updateStats, 2000);
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
