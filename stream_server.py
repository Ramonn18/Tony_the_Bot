from flask import Flask, Response, jsonify, send_from_directory
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

# ADS7830 battery monitor (Freenove board at 0x48, I2C bus 1)
import smbus as _smbus_mod
try:
    _adc_bus = _smbus_mod.SMBus(1)
    ADC_AVAILABLE = True
except Exception:
    _adc_bus = None
    ADC_AVAILABLE = False

ADC_ADDR  = 0x48
ADC_COEFF = 3  # Freenove voltage divider coefficient

def _adc_read_channel(ch):
    cmd = 0x84 | ((((ch << 2) | (ch >> 1)) & 0x07) << 4)
    _adc_bus.write_byte(ADC_ADDR, cmd)
    time.sleep(0.01)
    v1 = _adc_bus.read_byte(ADC_ADDR)
    v2 = _adc_bus.read_byte(ADC_ADDR)
    raw = v1 if v1 == v2 else (v1 + v2) // 2
    return round(raw / 255.0 * 5 * ADC_COEFF, 2)

def read_battery_voltages():
    if not ADC_AVAILABLE:
        return None, None
    try:
        return _adc_read_channel(0), _adc_read_channel(4)
    except Exception:
        return None, None

# 2S 18650 pack: 8.4V full, 6.0V empty
SOC_CURVE = [
    (8.40, 100.0), (8.20, 95.0), (8.00, 88.0), (7.80, 78.0),
    (7.60,  65.0), (7.40, 50.0), (7.20, 35.0), (7.00, 20.0),
    (6.80,  10.0), (6.40,  5.0), (6.00,  0.0),
]
BATTERY_CAPACITY_MAH = 5000  # 2× 2500 mAh 18650

def voltage_to_soc(v):
    for i, (vt, pct) in enumerate(SOC_CURVE):
        if v >= vt:
            if i == 0:
                return 100.0
            v_hi, p_hi = SOC_CURVE[i - 1]
            v_lo, p_lo = vt, pct
            return round(p_lo + (v - v_lo) / (v_hi - v_lo) * (p_hi - p_lo), 1)
    return 0.0

def estimate_runtime(soc, current_ma=500):
    remaining_mah = (soc / 100.0) * BATTERY_CAPACITY_MAH
    if current_ma and current_ma > 0:
        return round(remaining_mah / current_ma * 60)
    return None

app = Flask(__name__)

cam = None
model = None
cam_lock = threading.Lock()

raw_frame = None
raw_frame_lock = threading.Lock()

last_boxes = []
boxes_lock = threading.Lock()

latest_frame = None
frame_lock = threading.Lock()

fps_camera = 0.0
fps_stream = 0.0
fps_yolo   = 0.0

latest_detections = []
detection_history = deque(maxlen=100)
detections_lock = threading.Lock()
sse_clients = []
sse_lock = threading.Lock()


def start_camera():
    global cam
    cam = Picamera2()
    config = cam.create_video_configuration(
        main={"size": (1920, 1080), "format": "RGB888"},
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
    time.sleep(4)
    print("Camera started — 640x480 @ 30fps")


def load_model():
    global model
    model = YOLO("yolov8n.pt")
    model.fuse()
    print("YOLOv8n loaded and fused.")


def draw_boxes(frame, boxes):
    for (x1, y1, x2, y2, label) in boxes:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 255, 0), -1)
        cv2.putText(frame, label, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    return frame


def push_sse(data):
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
    global latest_frame, fps_stream
    count, t0 = 0, time.time()
    while True:
        try:
            with raw_frame_lock:
                frame = raw_frame
            if frame is None:
                time.sleep(0.01)
                continue
            bgr = frame  # treat RGB as BGR — swaps R↔B in stream per user preference
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
            time.sleep(0.0417)
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
        time.sleep(0.0417)


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
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory()
        ram_pct   = ram.percent
        ram_used  = round(ram.used / 1024 / 1024)
        ram_total = round(ram.total / 1024 / 1024)
    except Exception:
        cpu, ram_pct, ram_used, ram_total = None, None, None, None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp_c = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        temp_c = None
    try:
        with open("/sys/class/hwmon/hwmon2/fan1_input") as f:
            fan_rpm = int(f.read().strip())
    except Exception:
        fan_rpm = None
    return {
        "cpu_pct":   cpu,
        "ram_pct":   ram_pct,
        "ram_used":  ram_used,
        "ram_total": ram_total,
        "temp_c":    temp_c,
        "fan_rpm":   fan_rpm,
    }


def get_signal_strength():
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if not parts:
                continue
            dbm = float(parts[3].strip("."))
            if dbm > 0:
                dbm -= 256
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
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True).strip()
        hex_val = int(out.split("=")[1], 16)
        result["throttle_raw"] = hex(hex_val)
        result["under_voltage"] = bool(hex_val & 0x1)
        result["throttled"] = bool(hex_val & 0x4)
        result["source"] = "wall"
    except Exception:
        pass
    if ADC_AVAILABLE:
        try:
            v1, v2 = read_battery_voltages()
            if v2 is not None:
                soc     = voltage_to_soc(v2)
                runtime = estimate_runtime(soc)
                result["voltage"]      = v2
                result["voltage_cell"] = v1
                result["battery_pct"]  = soc
                result["runtime_min"]  = runtime
                result["source"]       = "battery"
        except Exception:
            pass
    return jsonify(result)


@app.route("/events")
def events():
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


@app.route("/emotion_detection/<path:filename>")
def emotion_static(filename):
    return send_from_directory("emotion_detection", filename)


@app.route("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>// TONY_MONITOR</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --yellow:  #f5c518;
      --cyan:    #00d4ff;
      --magenta: #ff003c;
      --orange:  #ff8c00;
      --green:   #00e676;
      --bg:      #080e0a;
      --surface: #0b1410;
      --card:    #0e1a12;
      --border:  rgba(255,255,255,0.06);
      --borderb: rgba(255,255,255,0.11);
      --text:    #ddeee4;
      --text2:   #4a6655;
      --dim:     #1a2e20;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      background-image: radial-gradient(ellipse at 35% 25%, rgba(0,60,20,0.18) 0%, transparent 65%);
      color: var(--text);
      font-family: 'Inter', sans-serif;
      height: 100vh; display: flex; overflow: hidden;
    }
    body::after {
      content: ''; position: fixed; inset: 0;
      background: repeating-linear-gradient(to bottom,
        transparent 0px, transparent 3px, rgba(0,0,0,0.04) 3px, rgba(0,0,0,0.04) 4px);
      pointer-events: none; z-index: 9999;
    }

    /* ── LEFT NAV ── */
    .leftnav {
      width: 158px; background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex; flex-direction: column; flex-shrink: 0;
    }
    .nav-brand {
      padding: 16px 14px 13px;
      border-bottom: 1px solid var(--border);
    }
    .nav-brand-title {
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.78rem; color: var(--yellow); letter-spacing: 3px; display: block;
    }
    .nav-brand-sub {
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.44rem; color: var(--text2); letter-spacing: 1px;
      display: block; margin-top: 4px;
    }
    .nav-section { padding: 8px 6px; flex: 1; display: flex; flex-direction: column; gap: 1px; }
    .nav-group-label {
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.42rem; color: var(--text2); letter-spacing: 2px;
      padding: 6px 8px 3px; text-transform: uppercase;
    }
    .nav-item {
      display: flex; align-items: center; gap: 9px;
      padding: 7px 9px; border-radius: 5px;
      cursor: pointer; transition: all 0.15s; user-select: none;
    }
    .nav-item:hover { background: rgba(0,230,118,0.05); }
    .nav-item.active { background: rgba(245,197,24,0.09); }
    .nav-icon { font-size: 0.8rem; width: 15px; text-align: center; flex-shrink: 0; color: var(--text2); }
    .nav-text {
      font-family: 'Share Tech Mono', monospace; font-size: 0.56rem;
      color: var(--text2); letter-spacing: 1px; text-transform: uppercase;
    }
    .nav-item.active .nav-icon,
    .nav-item.active .nav-text { color: var(--yellow); }
    .nav-item:hover .nav-icon,
    .nav-item:hover .nav-text { color: var(--text); }
    .nav-bottom {
      padding: 8px 6px; border-top: 1px solid var(--border);
      display: flex; flex-direction: column; gap: 1px;
    }

    /* ── APP SHELL ── */
    .app { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }

    /* ── TOP BAR ── */
    .topbar {
      display: flex; align-items: center; gap: 10px;
      padding: 0 16px; height: 48px; flex-shrink: 0;
      background: var(--surface); border-bottom: 1px solid var(--border);
    }
    .brand-title {
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.72rem; color: var(--yellow); letter-spacing: 3px; white-space: nowrap;
    }
    #status-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: var(--dim); flex-shrink: 0;
      transition: background 0.3s, box-shadow 0.3s;
    }
    #status-dot.live { background: var(--green); box-shadow: 0 0 6px var(--green); }
    .h-spacer { flex: 1; }
    .counts { display: flex; gap: 4px; flex-wrap: wrap; }
    .count-badge {
      background: rgba(0,212,255,0.05); border: 1px solid rgba(0,212,255,0.14);
      border-radius: 4px; padding: 2px 7px;
      font-size: 0.56rem; color: var(--text2); font-family: 'Share Tech Mono', monospace;
    }
    .count-badge span { color: var(--cyan); }
    .hbtn {
      background: transparent; border: 1px solid var(--borderb);
      color: var(--text2); font-family: 'Share Tech Mono', monospace;
      font-size: 0.56rem; letter-spacing: 1px; cursor: pointer;
      padding: 5px 10px; text-transform: uppercase; border-radius: 4px;
      transition: all 0.15s; flex-shrink: 0;
    }
    .hbtn:hover { color: var(--cyan); border-color: var(--cyan); }
    .hbtn.active { color: var(--yellow); border-color: var(--yellow); background: rgba(245,197,24,0.07); }
    .snap-btn {
      background: var(--yellow); border: none; color: #000;
      font-family: 'Share Tech Mono', monospace; font-size: 0.56rem;
      letter-spacing: 2px; cursor: pointer; text-transform: uppercase;
      padding: 5px 12px; border-radius: 4px; transition: opacity 0.15s; flex-shrink: 0;
    }
    .snap-btn:hover { opacity: 0.85; }
    .snap-btn.flash { background: #fff; }

    /* ── TAB BAR ── */
    .tabbar {
      display: flex; align-items: center;
      padding: 0 16px; height: 36px; flex-shrink: 0;
      background: var(--surface); border-bottom: 1px solid var(--border);
    }
    .tab {
      font-family: 'Share Tech Mono', monospace; font-size: 0.56rem; letter-spacing: 2px;
      color: var(--text2); padding: 0 14px; height: 100%;
      display: flex; align-items: center; cursor: pointer;
      text-transform: uppercase; border-bottom: 2px solid transparent;
      transition: all 0.15s; user-select: none;
    }
    .tab:hover { color: var(--text); }
    .tab.active { color: var(--yellow); border-bottom-color: var(--yellow); }

    /* ── SHORTCUTS OVERLAY ── */
    .shortcuts-overlay {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.88); z-index: 10000;
      align-items: center; justify-content: center;
    }
    .shortcuts-overlay.show { display: flex; }
    .shortcuts-box {
      background: var(--card); border: 1px solid var(--yellow);
      border-radius: 8px; padding: 22px 30px; min-width: 270px;
    }
    .shortcuts-box h2 {
      font-family: 'Share Tech Mono', monospace; color: var(--yellow);
      font-size: 0.75rem; letter-spacing: 3px; margin-bottom: 14px;
    }
    .shortcut-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 5px 0; border-bottom: 1px solid var(--border);
    }
    .shortcut-row:last-child { border-bottom: none; }
    .kbd {
      background: var(--bg); border: 1px solid var(--text2);
      border-radius: 3px; padding: 2px 7px; color: var(--yellow);
      font-family: 'Share Tech Mono', monospace; font-size: 0.58rem; min-width: 28px; text-align: center;
    }
    .shortcut-desc { color: var(--text2); font-family: 'Share Tech Mono', monospace; font-size: 0.58rem; }

    /* ── CONTENT ── */
    .content { flex: 1; display: flex; overflow: hidden; min-height: 0; }

    /* ══════════════════════════════════════
       LEFT PANEL  (fixed ~42% width)
    ══════════════════════════════════════ */
    .left-panel {
      flex: 0 0 42%; min-width: 320px; max-width: 560px;
      display: flex; flex-direction: column;
      padding: 12px; gap: 9px; overflow: hidden;
    }

    /* ─ Total Detected card (AgentOS "Total Agent Tasks" style) ─ */
    .total-card {
      flex-shrink: 0; background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px 14px;
      display: flex; gap: 12px; align-items: flex-start;
      position: relative; overflow: hidden;
    }
    .total-card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, var(--yellow) 0%, transparent 100%);
    }
    .total-left { display: flex; flex-direction: column; gap: 2px; flex-shrink: 0; }
    .total-eyebrow {
      font-family: 'Share Tech Mono', monospace; font-size: 0.5rem;
      color: var(--text2); letter-spacing: 2px; text-transform: uppercase;
    }
    .total-number {
      font-family: 'Rajdhani', sans-serif; font-size: 2.4rem; font-weight: 700;
      color: var(--text); line-height: 1; letter-spacing: -1px;
    }
    .total-delta {
      font-family: 'Share Tech Mono', monospace; font-size: 0.48rem;
      color: var(--green); letter-spacing: 1px;
    }
    .total-right { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }
    .hero-badge {
      display: inline-flex; align-items: center; gap: 5px;
      border-radius: 20px; padding: 3px 10px;
      font-family: 'Share Tech Mono', monospace; font-size: 0.55rem; letter-spacing: 1px;
      background: rgba(0,230,118,0.07); border: 1px solid rgba(0,230,118,0.2); color: var(--green);
    }
    .hero-badge.warn { background: rgba(255,140,0,0.07); border-color: rgba(255,140,0,0.2); color: var(--orange); }
    .hero-badge.crit { background: rgba(255,0,60,0.07); border-color: rgba(255,0,60,0.2); color: var(--magenta); animation: blink 1s step-start infinite; }
    #det-bar-canvas { width: 100%; display: block; }
    .frame-count {
      font-family: 'Share Tech Mono', monospace; font-size: 0.5rem; color: var(--text2);
      text-align: right;
    }
    .frame-count span { color: var(--cyan); font-family: 'Rajdhani', sans-serif; font-size: 1rem; font-weight: 600; }

    /* ─ Video ─ */
    .video-wrap {
      flex: 1; min-height: 0; position: relative;
      display: flex; align-items: center; justify-content: center;
      background: #030806; border: 1px solid var(--border);
      border-radius: 8px; overflow: hidden; cursor: pointer;
    }
    .video-wrap img {
      max-width: 100%; max-height: 100%; display: block;
      object-fit: contain; transition: opacity 0.2s;
    }
    .video-wrap.paused img { opacity: 0.2; }
    .corner {
      position: absolute; width: 16px; height: 16px;
      border-color: var(--yellow); border-style: solid;
      transition: border-color 0.3s; z-index: 2;
    }
    .corner.tl { top:8px; left:8px;    border-width: 2px 0 0 2px; }
    .corner.tr { top:8px; right:8px;   border-width: 2px 2px 0 0; }
    .corner.bl { bottom:8px; left:8px; border-width: 0 0 2px 2px; }
    .corner.br { bottom:8px; right:8px;border-width: 0 2px 2px 0; }
    .video-wrap.paused .corner { border-color: var(--magenta); }
    .pause-label {
      display: none; position: absolute; inset: 0;
      align-items: center; justify-content: center;
      font-family: 'Rajdhani', sans-serif; font-size: 1.2rem; font-weight: 700;
      letter-spacing: 4px; color: var(--magenta); pointer-events: none; z-index: 3;
    }
    .video-wrap.paused .pause-label { display: flex; }
    .video-wrap:fullscreen, .video-wrap:-webkit-full-screen {
      display: flex; align-items: center; justify-content: center;
      background: #000; width: 100%; height: 100%;
    }
    .video-wrap:fullscreen img, .video-wrap:-webkit-full-screen img { max-width:100%; max-height:100%; }

    /* ─ Stat strip ─ */
    .stat-strip {
      display: flex; flex-shrink: 0;
      border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
    }
    .strip-stat {
      flex: 1; padding: 6px 10px; background: var(--surface);
      display: flex; flex-direction: column; gap: 2px;
      border-right: 1px solid var(--border);
    }
    .strip-stat:last-child { border-right: none; }
    .strip-label { font-family: 'Share Tech Mono', monospace; font-size: 0.46rem; color: var(--text2); letter-spacing: 1px; text-transform: uppercase; }
    .strip-value { font-family: 'Rajdhani', sans-serif; font-size: 0.92rem; font-weight: 600; color: var(--text); }
    .strip-value.ok   { color: var(--cyan); }
    .strip-value.warn { color: var(--orange); }
    .strip-value.crit { color: var(--magenta); }

    /* ══════════════════════════════════════
       RIGHT COLUMN  (flex:1)
    ══════════════════════════════════════ */
    .right-col {
      flex: 1; min-width: 0;
      display: flex; flex-direction: column;
      padding: 12px 12px 12px 0; gap: 9px; overflow: hidden;
    }

    /* ─ Gauge Row — 3 speedometer arc gauges (AgentOS style) ─ */
    .gauge-row { display: flex; gap: 8px; flex-shrink: 0; }
    .gauge-card {
      flex: 1; background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 10px 8px 8px;
      display: flex; flex-direction: column; align-items: center; gap: 2px;
      position: relative; overflow: hidden; min-width: 0;
    }
    .gauge-card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    }
    .gauge-card.g-cpu::before  { background: linear-gradient(90deg, var(--yellow)  0%, transparent 100%); }
    .gauge-card.g-ram::before  { background: linear-gradient(90deg, var(--cyan)    0%, transparent 100%); }
    .gauge-card.g-temp::before { background: linear-gradient(90deg, var(--magenta) 0%, transparent 100%); }
    /* subtle inner glow matching each gauge */
    .gauge-card.g-cpu  { box-shadow: inset 0 -1px 20px rgba(245,197,24,0.04); }
    .gauge-card.g-ram  { box-shadow: inset 0 -1px 20px rgba(0,212,255,0.04); }
    .gauge-card.g-temp { box-shadow: inset 0 -1px 20px rgba(255,0,60,0.04); }
    .gauge-eyebrow {
      font-family: 'Share Tech Mono', monospace; font-size: 0.46rem;
      color: var(--text2); letter-spacing: 2px; text-transform: uppercase;
    }
    .gauge-svg { width: 100%; max-width: 110px; display: block; }

    /* ─ Cards Row ─ */
    .cards-row { display: flex; gap: 8px; flex-shrink: 0; }
    .card {
      flex: 1; background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px;
      display: flex; flex-direction: column; gap: 6px;
      position: relative; overflow: hidden; min-width: 0;
    }
    .card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    }
    .card-scan::before { background: linear-gradient(90deg, var(--cyan) 0%, transparent 100%); }
    .card-sys::before  { background: linear-gradient(90deg, var(--yellow) 0%, transparent 100%); }
    .card-eyebrow {
      font-family: 'Share Tech Mono', monospace; font-size: 0.5rem;
      color: var(--text2); letter-spacing: 2px; text-transform: uppercase;
    }
    .card-title {
      font-family: 'Rajdhani', sans-serif; font-size: 0.9rem; font-weight: 600;
      color: var(--text); line-height: 1.1;
    }
    .card-badge {
      display: inline-flex; align-items: center; gap: 4px; align-self: flex-start;
      border-radius: 4px; padding: 2px 7px;
      font-family: 'Share Tech Mono', monospace; font-size: 0.5rem; letter-spacing: 1px;
    }
    .card-badge.ok   { background: rgba(0,230,118,0.07); border: 1px solid rgba(0,230,118,0.2); color: var(--green); }
    .card-badge.warn { background: rgba(255,140,0,0.07); border: 1px solid rgba(255,140,0,0.2); color: var(--orange); }
    .card-badge.crit { background: rgba(255,0,60,0.07);  border: 1px solid rgba(255,0,60,0.2);  color: var(--magenta); }
    .card-badge.info { background: rgba(0,212,255,0.07); border: 1px solid rgba(0,212,255,0.2); color: var(--cyan); }
    .card-body { flex: 1; min-height: 0; }
    .card-action {
      background: transparent; border: 1px solid var(--borderb);
      color: var(--text2); font-family: 'Share Tech Mono', monospace;
      font-size: 0.5rem; letter-spacing: 1px; cursor: pointer;
      padding: 6px 10px; width: 100%; text-align: center;
      text-transform: uppercase; border-radius: 5px; transition: all 0.15s;
    }
    .card-scan .card-action:hover { color: var(--cyan);   border-color: var(--cyan); }
    .card-sys  .card-action:hover { color: var(--yellow); border-color: var(--yellow); }
    .tag {
      display: inline-block; margin: 2px 2px 2px 0; padding: 1px 6px;
      font-family: 'Share Tech Mono', monospace; font-size: 0.52rem; border-radius: 3px;
    }
    .tag-person { background: rgba(255,0,60,0.1); color: #ff6688; border: 1px solid rgba(255,0,60,0.25); }
    .tag-object { background: rgba(0,212,255,0.07); color: var(--cyan); border: 1px solid rgba(0,212,255,0.2); }
    .no-detect  { font-family: 'Share Tech Mono', monospace; font-size: 0.55rem; color: var(--text2); }
    .kv-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 4px 0; border-bottom: 1px solid var(--border);
    }
    .kv-row:last-child { border-bottom: none; }
    .kv-label { font-family: 'Share Tech Mono', monospace; font-size: 0.46rem; color: var(--text2); letter-spacing: 1px; }
    .kv-val   { font-family: 'Rajdhani', sans-serif; font-size: 0.82rem; font-weight: 600; color: var(--text); }
    .kv-val.ok   { color: var(--cyan); }
    .kv-val.warn { color: var(--orange); }
    .kv-val.crit { color: var(--magenta); }
    .batt-wrap { height: 3px; background: var(--dim); border-radius: 2px; overflow: hidden; margin-top: 3px; }
    .batt-bar  { height: 100%; border-radius: 2px; transition: width 0.5s, background 0.5s; }

    /* ─ Bottom card — Activity Distribution + sparkline + event log ─ */
    .bottom-card {
      flex: 1; min-height: 0; background: var(--card);
      border: 1px solid var(--border); border-radius: 10px;
      padding: 12px; display: flex; flex-direction: column; gap: 7px;
    }
    .bc-header { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
    .bc-title {
      font-family: 'Share Tech Mono', monospace; font-size: 0.5rem;
      color: var(--text2); letter-spacing: 2px; text-transform: uppercase;
    }
    .bc-meta { font-family: 'Share Tech Mono', monospace; font-size: 0.46rem; color: var(--text2); }
    .bc-actions { margin-left: auto; display: flex; gap: 4px; }
    .mini-btn {
      background: none; border: 1px solid var(--border);
      color: var(--text2); font-family: 'Share Tech Mono', monospace;
      font-size: 0.46rem; letter-spacing: 1px; cursor: pointer;
      padding: 2px 7px; text-transform: uppercase; border-radius: 3px; transition: all 0.15s;
    }
    .mini-btn:hover { color: var(--cyan); border-color: var(--cyan); }

    /* AgentOS primary metric */
    .perf-row { display: flex; align-items: baseline; gap: 7px; flex-shrink: 0; }
    .perf-big { font-family: 'Rajdhani', sans-serif; font-size: 1.8rem; font-weight: 700; color: var(--text); line-height: 1; }
    .perf-unit { font-family: 'Share Tech Mono', monospace; font-size: 0.54rem; color: var(--text2); }
    .perf-badge {
      font-family: 'Share Tech Mono', monospace; font-size: 0.46rem; letter-spacing: 1px;
      padding: 1px 6px; border-radius: 3px; margin-left: 3px;
    }
    .perf-badge.ok   { background: rgba(0,230,118,0.07); color: var(--green); border: 1px solid rgba(0,230,118,0.18); }
    .perf-badge.warn { background: rgba(255,140,0,0.07); color: var(--orange); border: 1px solid rgba(255,140,0,0.18); }
    .perf-badge.crit { background: rgba(255,0,60,0.07);  color: var(--magenta); border: 1px solid rgba(255,0,60,0.18); }
    .perf-aside { margin-left: auto; font-family: 'Share Tech Mono', monospace; font-size: 0.5rem; color: var(--text2); }

    #spark-canvas { width: 100%; flex-shrink: 0; display: block; }

    /* Activity Distribution grid — 20 cols × 3 rows, like AgentOS colored squares */
    .activity-grid {
      display: grid;
      grid-template-columns: repeat(20, 1fr);
      grid-template-rows: repeat(3, 1fr);
      gap: 3px; flex-shrink: 0;
    }
    .ag-cell {
      aspect-ratio: 1; border-radius: 2px;
      background: var(--dim); transition: background 0.35s;
    }
    /* row 0: detection (person=yellow, object=cyan) */
    .ag-cell.r0-person { background: var(--yellow); }
    .ag-cell.r0-object { background: var(--cyan); opacity: 0.75; }
    /* row 1: cpu activity */
    .ag-cell.r1-hot  { background: var(--orange); }
    .ag-cell.r1-warm { background: var(--orange); opacity: 0.45; }
    /* row 2: temp activity */
    .ag-cell.r2-hot  { background: var(--magenta); }
    .ag-cell.r2-warm { background: var(--magenta); opacity: 0.4; }
    /* row legends */
    .ag-legend {
      display: flex; gap: 10px; flex-shrink: 0;
    }
    .ag-dot {
      display: inline-block; width: 6px; height: 6px; border-radius: 1px; margin-right: 3px;
      vertical-align: middle;
    }
    .ag-legend-item { font-family: 'Share Tech Mono', monospace; font-size: 0.44rem; color: var(--text2); }

    #event-log { flex: 1; min-height: 0; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--dim) transparent; }
    .ev-item { padding: 4px 0; border-bottom: 1px solid var(--border); animation: fadeIn 0.2s ease; }
    @keyframes fadeIn { from { opacity:0; transform:translateY(-2px); } to { opacity:1; } }
    .ev-time { font-family: 'Share Tech Mono', monospace; font-size: 0.48rem; color: var(--text2); margin-bottom: 2px; }
    .ev-empty { font-family: 'Share Tech Mono', monospace; font-size: 0.54rem; color: var(--dim); font-style: italic; }

    @keyframes blink { 50% { opacity: 0; } }
  </style>
  <link rel="stylesheet" href="/emotion_detection/emotion_styles.css">
  <script src="https://unpkg.com/ml5@1/dist/ml5.min.js"></script>
</head>
<body>

  <!-- shortcuts overlay -->
  <div class="shortcuts-overlay" id="shortcuts-overlay" onclick="toggleShortcuts()">
    <div class="shortcuts-box" onclick="event.stopPropagation()">
      <h2>// KEYBOARD SHORTCUTS</h2>
      <div class="shortcut-row"><span class="kbd">S</span><span class="shortcut-desc">Take snapshot</span></div>
      <div class="shortcut-row"><span class="kbd">F</span><span class="shortcut-desc">Toggle fullscreen</span></div>
      <div class="shortcut-row"><span class="kbd">Space</span><span class="shortcut-desc">Pause / resume stream</span></div>
      <div class="shortcut-row"><span class="kbd">A</span><span class="shortcut-desc">Toggle sound alerts</span></div>
      <div class="shortcut-row"><span class="kbd">C</span><span class="shortcut-desc">Clear event log</span></div>
      <div class="shortcut-row"><span class="kbd">E</span><span class="shortcut-desc">Export event log</span></div>
      <div class="shortcut-row"><span class="kbd">M</span><span class="shortcut-desc">Toggle emotion detection</span></div>
      <div class="shortcut-row"><span class="kbd">?</span><span class="shortcut-desc">Show / hide shortcuts</span></div>
      <div class="shortcut-row"><span class="kbd">Esc</span><span class="shortcut-desc">Close overlay</span></div>
    </div>
  </div>

  <!-- ── LEFT NAV ── -->
  <nav class="leftnav">
    <div class="nav-brand">
      <span class="nav-brand-title">// TONY</span>
      <span class="nav-brand-sub">NEURAL SURVEILLANCE v3.2</span>
    </div>
    <div class="nav-section">
      <span class="nav-group-label">Monitor</span>
      <div class="nav-item active">
        <span class="nav-icon">&#9673;</span>
        <span class="nav-text">Live Feed</span>
      </div>
      <div class="nav-item">
        <span class="nav-icon">&#8853;</span>
        <span class="nav-text">Detection</span>
      </div>
      <div class="nav-item">
        <span class="nav-icon">&#8779;</span>
        <span class="nav-text">System</span>
      </div>
      <span class="nav-group-label" style="margin-top:6px;">Tools</span>
      <div class="nav-item">
        <span class="nav-icon">&#8801;</span>
        <span class="nav-text">Event Log</span>
      </div>
      <div class="nav-item" onclick="takeSnapshot()">
        <span class="nav-icon">&#9635;</span>
        <span class="nav-text">Snapshot</span>
      </div>
    </div>
    <div class="nav-bottom">
      <div class="nav-item" onclick="toggleShortcuts()">
        <span class="nav-icon">&#9881;</span>
        <span class="nav-text">Shortcuts</span>
      </div>
    </div>
  </nav>

  <!-- ── APP ── -->
  <div class="app">

    <!-- TOP BAR -->
    <div class="topbar">
      <span class="brand-title">TONY_MONITOR</span>
      <div id="status-dot"></div>
      <div class="h-spacer"></div>
      <div class="counts" id="counts"></div>
      <button class="hbtn" id="pause-btn" onclick="togglePause()">&#9646;&#9646; PAUSE</button>
      <button class="hbtn" id="sound-btn" onclick="toggleSound()">&#128264; SOUND</button>
      <button class="snap-btn" id="snap-btn" onclick="takeSnapshot()">&#9654; SNAPSHOT</button>
    </div>

    <!-- TAB BAR -->
    <div class="tabbar">
      <div class="tab active">Overview</div>
      <div class="tab">Neural</div>
      <div class="tab">System</div>
      <div class="tab">Events</div>
    </div>

    <!-- CONTENT -->
    <div class="content">

      <!-- ══ LEFT PANEL ══ -->
      <div class="left-panel">

        <!-- Total Detected — AgentOS "Total Agent Tasks Processed" style -->
        <div class="total-card">
          <div class="total-left">
            <span class="total-eyebrow">Total Objects Detected</span>
            <span class="total-number" id="total-detections">0</span>
            <span class="total-delta" id="total-delta">&#9650; session active</span>
          </div>
          <div class="total-right">
            <span class="hero-badge" id="hero-badge">&#9679; SCANNING</span>
            <canvas id="det-bar-canvas" height="38"></canvas>
            <span class="frame-count">IN FRAME &nbsp;<span id="frame-count-num">0</span></span>
          </div>
        </div>

        <!-- Live video -->
        <div class="video-wrap" id="video-wrap" onclick="toggleFullscreen()">
          <div class="corner tl"></div><div class="corner tr"></div>
          <div class="corner bl"></div><div class="corner br"></div>
          <img id="stream-img" src="/video_feed" alt="Live feed">
          <div class="pause-label">&#9646;&#9646; PAUSED</div>
        </div>

        <!-- Stat strip -->
        <div class="stat-strip">
          <div class="strip-stat">
            <span class="strip-label">Cam FPS</span>
            <span class="strip-value ok" id="fps-cam">--</span>
          </div>
          <div class="strip-stat">
            <span class="strip-label">Stream FPS</span>
            <span class="strip-value ok" id="fps-stream">--</span>
          </div>
          <div class="strip-stat">
            <span class="strip-label">AI FPS</span>
            <span class="strip-value" id="fps-yolo">--</span>
          </div>
          <div class="strip-stat">
            <span class="strip-label">Temp</span>
            <span class="strip-value" id="strip-temp">-- &deg;C</span>
          </div>
          <div class="strip-stat">
            <span class="strip-label">Uptime</span>
            <span class="strip-value" id="uptime-val">0m 0s</span>
          </div>
        </div>
      </div>

      <!-- ══ RIGHT COLUMN ══ -->
      <div class="right-col">

        <!-- GAUGE ROW — speedometer arc gauges (AgentOS "Agent Idle / Active / Verification") -->
        <div class="gauge-row">

          <!-- CPU gauge -->
          <div class="gauge-card g-cpu">
            <span class="gauge-eyebrow">CPU Load</span>
            <svg class="gauge-svg" viewBox="0 0 100 72">
              <!-- 300° arc: start (7 o'clock) M 18,64  end (5 o'clock) 82,64  radius 40 center 50,46 -->
              <path d="M 18,64 A 40,40 0 1 1 82,64"
                fill="none" stroke="var(--dim)" stroke-width="6" stroke-linecap="round"/>
              <path id="gauge-cpu-arc" d="M 18,64 A 40,40 0 1 1 82,64"
                fill="none" stroke="var(--yellow)" stroke-width="6" stroke-linecap="round"
                stroke-dasharray="0 209.4" style="transition:stroke-dasharray 0.6s ease;"/>
              <text id="gauge-cpu-val" x="50" y="50" text-anchor="middle"
                font-family="Rajdhani, sans-serif" font-size="17" font-weight="700" fill="#ddeee4">--%</text>
              <text id="gauge-cpu-sub" x="50" y="63" text-anchor="middle"
                font-family="Share Tech Mono, monospace" font-size="5.5" fill="#4a6655">PROCESSOR</text>
            </svg>
          </div>

          <!-- RAM gauge -->
          <div class="gauge-card g-ram">
            <span class="gauge-eyebrow">RAM Usage</span>
            <svg class="gauge-svg" viewBox="0 0 100 72">
              <path d="M 18,64 A 40,40 0 1 1 82,64"
                fill="none" stroke="var(--dim)" stroke-width="6" stroke-linecap="round"/>
              <path id="gauge-ram-arc" d="M 18,64 A 40,40 0 1 1 82,64"
                fill="none" stroke="var(--cyan)" stroke-width="6" stroke-linecap="round"
                stroke-dasharray="0 209.4" style="transition:stroke-dasharray 0.6s ease;"/>
              <text id="gauge-ram-val" x="50" y="50" text-anchor="middle"
                font-family="Rajdhani, sans-serif" font-size="17" font-weight="700" fill="#ddeee4">--%</text>
              <text id="gauge-ram-sub" x="50" y="63" text-anchor="middle"
                font-family="Share Tech Mono, monospace" font-size="5.5" fill="#4a6655">MEMORY</text>
            </svg>
          </div>

          <!-- TEMP + FAN gauge -->
          <div class="gauge-card g-temp" style="flex:2;">
            <div style="display:flex;width:100%;gap:6px;align-items:flex-start;">
              <div style="flex:1;display:flex;flex-direction:column;align-items:center;">
                <span class="gauge-eyebrow">CPU Temp</span>
                <svg style="width:100%;max-width:110px;display:block;" viewBox="0 0 100 72">
                  <path d="M 18,64 A 40,40 0 1 1 82,64"
                    fill="none" stroke="var(--dim)" stroke-width="6" stroke-linecap="round"/>
                  <path id="gauge-temp-arc" d="M 18,64 A 40,40 0 1 1 82,64"
                    fill="none" stroke="var(--magenta)" stroke-width="6" stroke-linecap="round"
                    stroke-dasharray="0 209.4" style="transition:stroke-dasharray 0.6s ease;"/>
                  <text id="gauge-temp-val" x="50" y="50" text-anchor="middle"
                    font-family="Rajdhani, sans-serif" font-size="17" font-weight="700" fill="#ddeee4">--&deg;</text>
                  <text x="50" y="63" text-anchor="middle"
                    font-family="Share Tech Mono, monospace" font-size="5.5" fill="#4a6655">THERMAL</text>
                </svg>
              </div>
              <div style="flex:1;display:flex;flex-direction:column;align-items:center;">
                <span class="gauge-eyebrow">Fan Speed</span>
                <svg style="width:100%;max-width:110px;display:block;" viewBox="0 0 100 72">
                  <path d="M 18,64 A 40,40 0 1 1 82,64"
                    fill="none" stroke="var(--dim)" stroke-width="6" stroke-linecap="round"/>
                  <path id="gauge-fan-arc" d="M 18,64 A 40,40 0 1 1 82,64"
                    fill="none" stroke="var(--cyan)" stroke-width="6" stroke-linecap="round"
                    stroke-dasharray="0 209.4" style="transition:stroke-dasharray 0.6s ease;"/>
                  <text id="fan-rpm" x="50" y="48" text-anchor="middle"
                    font-family="Rajdhani, sans-serif" font-size="13" font-weight="700" fill="#ddeee4">--</text>
                  <text x="50" y="59" text-anchor="middle"
                    font-family="Share Tech Mono, monospace" font-size="5" fill="#4a6655">RPM</text>
                  <text id="fan-rpm-sub" x="50" y="68" text-anchor="middle"
                    font-family="Share Tech Mono, monospace" font-size="5" fill="#4a6655">FAN</text>
                </svg>
              </div>
            </div>
          </div>

        </div><!-- /gauge-row -->

        <!-- CARDS ROW: Neural Scan + System Status -->
        <div class="cards-row">

          <div class="card card-scan">
            <span class="card-eyebrow">&#8853; Neural Scan</span>
            <span class="card-title">Object Detection</span>
            <span class="card-badge info" id="scan-status">SCANNING</span>
            <div class="card-body" id="scan-tags">
              <span class="no-detect">Awaiting objects...</span>
            </div>
            <div style="font-family:'Share Tech Mono',monospace;font-size:0.46rem;color:var(--text2);" id="scan-time">&mdash;</div>
            <button class="card-action" onclick="clearLog()">Clear Log &rarr;</button>
          </div>

          <div class="card card-sys">
            <span class="card-eyebrow">&#8779; System</span>
            <span class="card-title">Health Status</span>
            <span class="card-badge ok" id="sys-status">NOMINAL</span>
            <div class="card-body">
              <div class="kv-row">
                <span class="kv-label">WiFi</span>
                <span class="kv-val ok" id="signal-qual">--%</span>
              </div>
              <div class="kv-row">
                <span class="kv-label">Signal</span>
                <span class="kv-val" style="font-family:'Share Tech Mono',monospace;font-size:0.5rem;" id="signal-dbm">-- dBm</span>
              </div>
              <div class="kv-row">
                <span class="kv-label">Power</span>
                <span class="kv-val ok" id="power-status">--</span>
              </div>
              <div class="kv-row">
                <span class="kv-label">Battery</span>
                <span class="kv-val" id="battery-pct-chip">N/A</span>
              </div>
              <div id="battery-bar-wrap" class="batt-wrap" style="display:none">
                <div id="battery-bar" class="batt-bar"></div>
              </div>
              <div class="kv-row">
                <span class="kv-label">Voltage</span>
                <span class="kv-val" id="batt-voltage">--</span>
              </div>
              <div class="kv-row">
                <span class="kv-label">Runtime</span>
                <span class="kv-val ok" id="batt-runtime">--</span>
              </div>
              <div class="kv-row">
                <span class="kv-label">Current</span>
                <span class="kv-val" style="font-family:'Share Tech Mono',monospace;font-size:0.46rem;color:var(--text2);">N/A (ADC only)</span>
              </div>
              <div class="kv-row">
                <span class="kv-label">Throttle</span>
                <span class="kv-val ok" id="throttle-status">--</span>
              </div>
            </div>
            <button class="card-action" onclick="toggleShortcuts()">Diagnostics &rarr;</button>
          </div>

        </div><!-- /cards-row -->

        <!-- BOTTOM CARD: Activity Distribution + sparkline + event log -->
        <div class="bottom-card">
          <div class="bc-header">
            <span class="bc-title">Activity Distribution</span>
            <span class="bc-meta" id="bc-meta">Active Coverage</span>
            <div class="bc-actions">
              <button class="mini-btn" onclick="exportLog()">&#8615; Export</button>
              <button class="mini-btn" onclick="clearLog()">&#10005; Clear</button>
            </div>
          </div>

          <div class="perf-row">
            <span class="perf-big" id="perf-big">--</span>
            <span class="perf-unit">% CPU</span>
            <span class="perf-badge ok" id="perf-badge">NOMINAL</span>
            <span class="perf-aside" id="perf-aside">-- &deg;C</span>
          </div>

          <canvas id="spark-canvas" height="36"></canvas>

          <!-- Multi-row activity grid (like AgentOS colored squares) -->
          <div class="activity-grid" id="activity-grid"></div>
          <div class="ag-legend">
            <span class="ag-legend-item"><span class="ag-dot" style="background:var(--yellow)"></span>Person</span>
            <span class="ag-legend-item"><span class="ag-dot" style="background:var(--cyan)"></span>Object</span>
            <span class="ag-legend-item"><span class="ag-dot" style="background:var(--orange)"></span>CPU hi</span>
            <span class="ag-legend-item"><span class="ag-dot" style="background:var(--magenta)"></span>Temp hi</span>
          </div>

          <div id="event-log"></div>
        </div>

      </div><!-- /right-col -->
    </div><!-- /content -->
  </div><!-- /app -->

  <script>
    // ── STATE ──
    const logEl     = document.getElementById('event-log');
    const countsEl  = document.getElementById('counts');
    const dot       = document.getElementById('status-dot');
    const streamImg = document.getElementById('stream-img');
    const videoWrap = document.getElementById('video-wrap');
    const counts    = {};
    let eventHistory  = [];
    let totalDetected = 0;
    let soundEnabled  = false;
    let streamPaused  = false;
    let audioCtx      = null;
    const startTime   = Date.now();

    // Sparkline
    const SPARK_N = 45;
    const spark   = { cpu: [], ram: [], temp: [] };
    const FAN_MAX = 10000;

    // Gauge arc: M 18,64 A 40,40 0 1 1 82,64  radius=40, 300° arc
    // Arc length = 2*PI*40*(300/360) = 209.4
    const GAUGE_CIRC = 209.4;

    // Detection bar chart: 20 buckets, one per 4 seconds
    const BAR_N       = 20;
    const barData     = new Array(BAR_N).fill(0);
    const barPersonData = new Array(BAR_N).fill(0);
    let barLastTime   = Date.now();

    // Activity distribution grid: 20 columns × 3 rows
    const AG_COLS = 20;
    const AG_ROWS = 3; // row0=detection, row1=cpu, row2=temp
    // Each column is one time slot (4 seconds)
    // Values: row0: 0=clear,1=object,2=person | row1: 0=ok,1=warm,2=hot | row2: 0=ok,1=warm,2=hot
    const agData = Array.from({length: AG_ROWS}, () => new Array(AG_COLS).fill(0));
    const agGrid = document.getElementById('activity-grid');
    // Build 60 cells: cols change fastest (left→right), rows after
    for (let r = 0; r < AG_ROWS; r++) {
      for (let c = 0; c < AG_COLS; c++) {
        const d = document.createElement('div');
        d.className = 'ag-cell';
        agGrid.appendChild(d);
      }
    }

    let lastCpu  = 0;
    let lastTemp = 0;

    // ── UPTIME ──
    setInterval(() => {
      const s  = Math.floor((Date.now() - startTime) / 1000);
      const h  = Math.floor(s / 3600);
      const m  = Math.floor((s % 3600) / 60);
      const ss = s % 60;
      document.getElementById('uptime-val').textContent =
        (h ? h + 'h ' : '') + m + 'm ' + ss + 's';
    }, 1000);

    // ── SOUND ──
    function getAudioCtx() {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      return audioCtx;
    }
    function playBeep(freq, dur, vol) {
      freq = freq || 880; dur = dur || 0.08; vol = vol || 0.18;
      try {
        const ctx = getAudioCtx(), osc = ctx.createOscillator(), gain = ctx.createGain();
        osc.connect(gain); gain.connect(ctx.destination);
        osc.frequency.value = freq; osc.type = 'square';
        gain.gain.setValueAtTime(vol, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
        osc.start(ctx.currentTime); osc.stop(ctx.currentTime + dur);
      } catch(e) {}
    }
    function toggleSound() {
      soundEnabled = !soundEnabled;
      const btn = document.getElementById('sound-btn');
      btn.classList.toggle('active', soundEnabled);
      btn.innerHTML = soundEnabled ? '&#128266; SOUND' : '&#128264; SOUND';
      if (soundEnabled) playBeep(660, 0.06);
    }

    // ── PAUSE ──
    function togglePause() {
      streamPaused = !streamPaused;
      const btn = document.getElementById('pause-btn');
      if (streamPaused) {
        streamImg.src = '';
        videoWrap.classList.add('paused');
        btn.classList.add('active');
        btn.innerHTML = '&#9654; RESUME';
      } else {
        streamImg.src = '/video_feed';
        videoWrap.classList.remove('paused');
        btn.classList.remove('active');
        btn.innerHTML = '&#9646;&#9646; PAUSE';
      }
    }

    // ── FULLSCREEN ──
    function toggleFullscreen() {
      if (!document.fullscreenElement && !document.webkitFullscreenElement)
        (videoWrap.requestFullscreen || videoWrap.webkitRequestFullscreen).call(videoWrap);
      else
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }

    // ── SHORTCUTS ──
    function toggleShortcuts() {
      document.getElementById('shortcuts-overlay').classList.toggle('show');
    }
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      switch (e.key.toLowerCase()) {
        case 's':      takeSnapshot();     break;
        case 'f':      toggleFullscreen(); break;
        case ' ':      e.preventDefault(); togglePause(); break;
        case 'a':      toggleSound();      break;
        case 'c':      clearLog();         break;
        case 'e':      exportLog();        break;
        case '?':      toggleShortcuts();  break;
        case 'escape': document.getElementById('shortcuts-overlay').classList.remove('show'); break;
      }
    });

    // ── SPEEDOMETER GAUGES ──
    function setGauge(arcId, pct) {
      const arc  = document.getElementById(arcId);
      const dash = Math.min(1, pct / 100) * GAUGE_CIRC;
      arc.setAttribute('stroke-dasharray', dash.toFixed(1) + ' ' + GAUGE_CIRC);
    }

    // ── DETECTION BAR CHART (canvas, AgentOS style column chart) ──
    const barCanvas = document.getElementById('det-bar-canvas');
    const barCtx    = barCanvas.getContext('2d');

    function drawBarChart() {
      const W = barCanvas.offsetWidth, H = 38;
      barCanvas.width = W; barCanvas.height = H;
      barCtx.clearRect(0, 0, W, H);
      const maxVal = Math.max(1, ...barData);
      const barW   = (W / BAR_N) - 2;
      for (let i = 0; i < BAR_N; i++) {
        const x   = i * (W / BAR_N) + 1;
        const h   = (barData[i] / maxVal) * (H - 4);
        const y   = H - h - 2;
        const isP = barPersonData[i] > 0;
        barCtx.fillStyle = i === BAR_N - 1 ? 'rgba(245,197,24,0.9)'
          : isP ? 'rgba(255,0,60,0.55)' : 'rgba(0,212,255,0.35)';
        if (h > 0) {
          barCtx.beginPath();
          barCtx.roundRect ? barCtx.roundRect(x, y, barW, h, 1) : barCtx.rect(x, y, barW, h);
          barCtx.fill();
        } else {
          barCtx.fillStyle = 'rgba(255,255,255,0.06)';
          barCtx.fillRect(x, H - 3, barW, 2);
        }
      }
    }

    function tickBarBucket(ev) {
      const now = Date.now();
      if (now - barLastTime > 4000) {
        barData.push(0);
        barPersonData.push(0);
        if (barData.length > BAR_N) { barData.shift(); barPersonData.shift(); }
        barLastTime = now;
      }
      if (ev.objects.length > 0) {
        barData[barData.length - 1] += ev.objects.length;
        if (ev.objects.some(o => o.label === 'person'))
          barPersonData[barData.length - 1]++;
      }
      drawBarChart();
    }

    // ── ACTIVITY GRID ──
    function tickActivityGrid(ev, cpu, temp) {
      // Advance column on each detection event (or on stats update)
    }

    function pushActivityCol(detVal, cpuVal, tempVal) {
      // Shift all columns left
      for (let r = 0; r < AG_ROWS; r++) {
        agData[r].push(r === 0 ? detVal : r === 1 ? cpuVal : tempVal);
        if (agData[r].length > AG_COLS) agData[r].shift();
      }
      // Redraw
      const cells = agGrid.children;
      for (let r = 0; r < AG_ROWS; r++) {
        for (let c = 0; c < AG_COLS; c++) {
          const cell = cells[r * AG_COLS + c];
          const v    = agData[r][c] || 0;
          let cls    = 'ag-cell';
          if (r === 0) {
            if (v === 2) cls += ' r0-person';
            else if (v === 1) cls += ' r0-object';
          } else if (r === 1) {
            if (v === 2) cls += ' r1-hot';
            else if (v === 1) cls += ' r1-warm';
          } else {
            if (v === 2) cls += ' r2-hot';
            else if (v === 1) cls += ' r2-warm';
          }
          cell.className = cls;
        }
      }
      // Update meta
      const hot = agData[0].filter(v => v > 0).length;
      const pct = Math.round((hot / AG_COLS) * 100);
      document.getElementById('bc-meta').textContent = 'Active Coverage: ' + pct + '%';
    }

    // push activity column every 4 seconds even without events
    setInterval(() => {
      const detVal  = 0;  // idle tick
      const cpuVal  = lastCpu  >= 90 ? 2 : lastCpu  >= 70 ? 1 : 0;
      const tempVal = lastTemp >= 80 ? 2 : lastTemp >= 70 ? 1 : 0;
      pushActivityCol(detVal, cpuVal, tempVal);
    }, 4000);

    // ── HERO + SCAN CARD ──
    function updateHero(objects) {
      document.getElementById('frame-count-num').textContent = objects.length;
      const badge      = document.getElementById('hero-badge');
      const scanStatus = document.getElementById('scan-status');
      const tagsEl     = document.getElementById('scan-tags');
      if (objects.length === 0) {
        badge.className = 'hero-badge';
        badge.innerHTML = '&#9679; CLEAR';
        scanStatus.className = 'card-badge info';
        scanStatus.textContent = 'SCANNING';
        tagsEl.innerHTML = '<span class="no-detect">No objects in frame</span>';
      } else {
        const hasPerson = objects.some(o => o.label === 'person');
        if (hasPerson) {
          badge.className = 'hero-badge crit';
          badge.innerHTML = '&#9679; PERSON DETECTED';
          scanStatus.className = 'card-badge crit';
          scanStatus.textContent = 'PERSON';
        } else {
          badge.className = 'hero-badge warn';
          badge.innerHTML = '&#9679; OBJECTS DETECTED';
          scanStatus.className = 'card-badge warn';
          scanStatus.textContent = 'OBJECTS';
        }
        tagsEl.innerHTML = objects.map(o =>
          '<span class="tag ' + (o.label === 'person' ? 'tag-person' : 'tag-object') + '">' +
          o.label + ' ' + Math.round(o.confidence * 100) + '%</span>'
        ).join('');
      }
    }

    // ── EVENTS ──
    function tagHTML(obj) {
      return '<span class="tag ' + (obj.label === 'person' ? 'tag-person' : 'tag-object') + '">' +
             obj.label + ' ' + Math.round(obj.confidence * 100) + '%</span>';
    }
    function updateCounts(objects) {
      objects.forEach(o => { counts[o.label] = (counts[o.label] || 0) + 1; });
      countsEl.innerHTML = Object.entries(counts)
        .sort((a,b) => b[1]-a[1]).slice(0,5)
        .map(([k,v]) => '<div class="count-badge">' + k + ' <span>' + v + '</span></div>').join('');
    }
    function addEvent(ev) {
      eventHistory.unshift(ev);
      if (eventHistory.length > 500) eventHistory.pop();

      if (ev.objects.length > 0) {
        totalDetected += ev.objects.length;
        document.getElementById('total-detections').textContent = totalDetected.toLocaleString();
        document.getElementById('total-delta').innerHTML =
          '&#9650; +' + ev.objects.length + ' &middot; ' + ev.timestamp;
      }

      updateHero(ev.objects);
      tickBarBucket(ev);
      document.getElementById('scan-time').textContent = 'Last: ' + ev.timestamp;

      // push to activity grid
      const hasPerson = ev.objects.some(o => o.label === 'person');
      const detVal    = ev.objects.length === 0 ? 0 : hasPerson ? 2 : 1;
      const cpuVal    = lastCpu  >= 90 ? 2 : lastCpu  >= 70 ? 1 : 0;
      const tempVal   = lastTemp >= 80 ? 2 : lastTemp >= 70 ? 1 : 0;
      pushActivityCol(detVal, cpuVal, tempVal);

      if (soundEnabled && ev.objects.length > 0)
        playBeep(hasPerson ? 1047 : 660, 0.07);

      const div = document.createElement('div');
      div.className = 'ev-item';
      if (ev.objects.length === 0) {
        div.innerHTML = '<div class="ev-time">' + ev.timestamp + '</div>' +
                        '<div class="ev-empty">// no objects</div>';
      } else {
        div.innerHTML = '<div class="ev-time">' + ev.timestamp + '</div>' +
                        ev.objects.map(tagHTML).join('');
        updateCounts(ev.objects);
      }
      logEl.prepend(div);
      while (logEl.children.length > 80) logEl.removeChild(logEl.lastChild);
    }

    function clearLog()  { logEl.innerHTML = ''; }
    function exportLog() {
      const ts   = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const blob = new Blob([JSON.stringify(eventHistory, null, 2)], { type: 'application/json' });
      const a    = Object.assign(document.createElement('a'),
                     { href: URL.createObjectURL(blob), download: 'tony_events_' + ts + '.json' });
      a.click(); URL.revokeObjectURL(a.href);
    }

    // ── SSE ──
    fetch('/detections').then(r => r.json()).then(d => {
      d.history.slice().reverse().forEach(addEvent);
      dot.classList.add('live');
    });
    const es = new EventSource('/events');
    es.onmessage = e => addEvent(JSON.parse(e.data));
    es.onerror   = () => dot.classList.remove('live');
    es.onopen    = () => dot.classList.add('live');

    // ── POWER ──
    function updatePower() {
      fetch('/power').then(r => r.json()).then(p => {
        const statusEl = document.getElementById('power-status');
        const barWrap  = document.getElementById('battery-bar-wrap');
        const bar      = document.getElementById('battery-bar');
        const pctChip  = document.getElementById('battery-pct-chip');
        if (p.under_voltage || p.throttled) {
          statusEl.textContent = p.throttled ? 'THROTTLED' : 'UNDER-V';
          statusEl.className = 'kv-val crit';
        } else if (p.source === 'wall' || p.source === 'unknown') {
          statusEl.textContent = 'AC POWER'; statusEl.className = 'kv-val ok';
        } else if (p.source === 'charging') {
          statusEl.textContent = 'CHARGING'; statusEl.className = 'kv-val ok';
        } else {
          statusEl.textContent = 'BATTERY'; statusEl.className = 'kv-val ok';
        }
        if (p.battery_pct != null) {
          const pct = p.battery_pct;
          barWrap.style.display = 'block';
          bar.style.width = pct + '%';
          bar.style.background = pct > 50 ? 'var(--cyan)' : pct > 20 ? 'var(--orange)' : 'var(--magenta)';
          pctChip.textContent = pct.toFixed(0) + '%';
          pctChip.className = 'kv-val ' + (pct > 50 ? 'ok' : pct > 20 ? 'warn' : 'crit');
        } else {
          barWrap.style.display = 'none';
          pctChip.textContent = 'N/A';
          pctChip.className = 'kv-val';
        }

        const voltEl    = document.getElementById('batt-voltage');
        const runtimeEl = document.getElementById('batt-runtime');
        const throttleEl = document.getElementById('throttle-status');

        if (p.voltage != null) {
          voltEl.textContent = p.voltage.toFixed(2) + 'V';
          voltEl.className = 'kv-val ' + (p.voltage > 7.0 ? 'ok' : p.voltage > 6.4 ? 'warn' : 'crit');
        }

        if (p.runtime_min != null) {
          const h = Math.floor(p.runtime_min / 60);
          const m = Math.round(p.runtime_min % 60);
          runtimeEl.textContent = (h > 0 ? h + 'h ' : '') + m + 'm';
          runtimeEl.className = 'kv-val ' + (p.runtime_min > 60 ? 'ok' : p.runtime_min > 20 ? 'warn' : 'crit');
        }

        if (p.throttled) {
          throttleEl.textContent = 'THROTTLED';
          throttleEl.className = 'kv-val crit';
        } else if (p.under_voltage) {
          throttleEl.textContent = 'UNDER-V';
          throttleEl.className = 'kv-val warn';
        } else {
          throttleEl.textContent = 'OK';
          throttleEl.className = 'kv-val ok';
        }
      }).catch(() => {});
    }
    updatePower(); setInterval(updatePower, 6000);

    // ── STATS + FILLED SPARKLINE ──
    const sparkCanvas = document.getElementById('spark-canvas');
    const ctx2        = sparkCanvas.getContext('2d');

    function drawSparkline() {
      const W = sparkCanvas.offsetWidth, H = 36;
      sparkCanvas.width = W; sparkCanvas.height = H;
      ctx2.clearRect(0, 0, W, H);
      [
        { data: spark.cpu,  stroke: '#f5c518', fill: 'rgba(245,197,24,0.12)',  label: 'CPU' },
        { data: spark.ram,  stroke: '#00d4ff', fill: 'rgba(0,212,255,0.08)',   label: 'RAM' },
        { data: spark.temp, stroke: '#ff003c', fill: 'rgba(255,0,60,0.07)',    label: 'TMP' },
      ].forEach(({ data, stroke, fill, label }, si) => {
        if (data.length < 2) return;
        const step = W / (SPARK_N - 1);
        const pts  = data.map((v, i) => ({
          x: (SPARK_N - data.length + i) * step,
          y: (H - 7) - (v / 100) * (H - 10)
        }));
        ctx2.beginPath(); ctx2.fillStyle = fill;
        pts.forEach((p, i) => i === 0 ? ctx2.moveTo(p.x, p.y) : ctx2.lineTo(p.x, p.y));
        ctx2.lineTo(pts[pts.length-1].x, H); ctx2.lineTo(pts[0].x, H); ctx2.closePath();
        ctx2.fill();
        ctx2.beginPath(); ctx2.strokeStyle = stroke; ctx2.lineWidth = 1.5; ctx2.globalAlpha = 0.85;
        pts.forEach((p, i) => i === 0 ? ctx2.moveTo(p.x, p.y) : ctx2.lineTo(p.x, p.y));
        ctx2.stroke(); ctx2.globalAlpha = 1;
        ctx2.fillStyle = stroke;
        ctx2.font = '8px Share Tech Mono, monospace';
        ctx2.fillText(label, 4 + si * 36, H - 1);
      });
    }

    function updateSysCard(cpu, temp) {
      const el = document.getElementById('sys-status');
      const pb = document.getElementById('perf-badge');
      if (temp > 80 || cpu > 90) {
        el.textContent = 'CRITICAL'; el.className = 'card-badge crit';
        pb.textContent = 'CRITICAL'; pb.className = 'perf-badge crit';
      } else if (temp > 70 || cpu > 75) {
        el.textContent = 'WARNING'; el.className = 'card-badge warn';
        pb.textContent = 'WARNING'; pb.className = 'perf-badge warn';
      } else {
        el.textContent = 'NOMINAL'; el.className = 'card-badge ok';
        pb.textContent = 'NOMINAL'; pb.className = 'perf-badge ok';
      }
    }

    function updateStats() {
      fetch('/stats').then(r => r.json()).then(s => {
        document.getElementById('fps-cam').textContent    = s.fps_camera != null ? s.fps_camera : '--';
        document.getElementById('fps-stream').textContent = s.fps_stream != null ? s.fps_stream : '--';

        if (s.fan_rpm != null) {
          const rpm = s.fan_rpm;
          const pct = Math.min(100, (rpm / FAN_MAX) * 100);
          const arc = document.getElementById('gauge-fan-arc');
          arc.setAttribute('stroke-dasharray', (pct / 100 * 209.4).toFixed(1) + ' 209.4');
          arc.setAttribute('stroke', rpm > 8000 ? 'var(--orange)' : 'var(--cyan)');
          document.getElementById('fan-rpm').textContent = rpm.toLocaleString();
          document.getElementById('fan-rpm-sub').textContent =
            rpm > 8000 ? 'HIGH' : rpm > 3000 ? 'NORMAL' : 'LOW';
        }
        const yEl = document.getElementById('fps-yolo');
        yEl.textContent = s.fps_yolo != null ? s.fps_yolo : '--';
        yEl.className   = 'strip-value ' + (s.fps_yolo >= 3 ? 'ok' : 'warn');

        if (s.cpu_pct != null) {
          lastCpu = s.cpu_pct;
          setGauge('gauge-cpu-arc', s.cpu_pct);
          document.getElementById('gauge-cpu-val').textContent = s.cpu_pct.toFixed(0) + '%';
          document.getElementById('gauge-cpu-sub').textContent =
            s.cpu_pct >= 90 ? 'CRITICAL' : s.cpu_pct >= 70 ? 'ELEVATED' : 'NOMINAL';
          document.getElementById('perf-big').textContent = s.cpu_pct.toFixed(0);
          spark.cpu.push(s.cpu_pct); if (spark.cpu.length > SPARK_N) spark.cpu.shift();
        }
        if (s.ram_pct != null) {
          setGauge('gauge-ram-arc', s.ram_pct);
          document.getElementById('gauge-ram-val').textContent = s.ram_pct.toFixed(0) + '%';
          document.getElementById('gauge-ram-sub').textContent =
            (s.ram_used || '--') + '/' + (s.ram_total || '--') + 'MB';
          spark.ram.push(s.ram_pct); if (spark.ram.length > SPARK_N) spark.ram.shift();
        }
        if (s.temp_c != null) {
          lastTemp = s.temp_c;
          const tp = Math.min(100, ((s.temp_c - 30) / 60) * 100);
          setGauge('gauge-temp-arc', tp);
          document.getElementById('gauge-temp-val').textContent = s.temp_c + '\u00b0';
          const stripTemp = document.getElementById('strip-temp');
          stripTemp.textContent = s.temp_c + ' \u00b0C';
          stripTemp.className   = 'strip-value ' + (tp >= 75 ? 'crit' : tp >= 60 ? 'warn' : 'ok');
          document.getElementById('perf-aside').textContent = s.temp_c + ' \u00b0C';
          spark.temp.push(tp); if (spark.temp.length > SPARK_N) spark.temp.shift();
          updateSysCard(s.cpu_pct, s.temp_c);
        }
        drawSparkline();

        if (s.signal_quality != null) {
          const qualEl = document.getElementById('signal-qual');
          qualEl.textContent = s.signal_quality + '%';
          qualEl.className   = 'kv-val ' + (s.signal_quality >= 60 ? 'ok' : s.signal_quality >= 35 ? 'warn' : 'crit');
          document.getElementById('signal-dbm').textContent = s.signal_dbm + ' dBm';
        }
      });
    }
    updateStats(); setInterval(updateStats, 2000);

    // ── SNAPSHOT ──
    function takeSnapshot() {
      const btn = document.getElementById('snap-btn');
      btn.classList.add('flash'); btn.textContent = '\u2713 CAPTURED';
      Object.assign(document.createElement('a'), { href: '/snapshot', download: '' }).click();
      setTimeout(() => { btn.classList.remove('flash'); btn.innerHTML = '&#9654; SNAPSHOT'; }, 1200);
    }

    // Initial bar draw
    drawBarChart();
  </script>
  <script src="/emotion_detection/emotion_engine.js"></script>
  <script src="/emotion_detection/facemesh_controller.js"></script>
  <script src="/emotion_detection/emotion_ui.js"></script>
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
