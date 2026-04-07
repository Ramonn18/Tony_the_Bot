from flask import Flask, Response
from picamera2 import Picamera2
from ultralytics import YOLO
import libcamera
import cv2
import threading
import time

app = Flask(__name__)

cam = None
model = None
cam_lock = threading.Lock()
latest_frame = None
frame_lock = threading.Lock()


def start_camera():
    global cam
    cam = Picamera2()
    config = cam.create_video_configuration(
        main={"size": (640, 480), "format": "BGR888"},
        controls={
            "FrameRate": 30,
            "AwbEnable": True,
            "AwbMode": libcamera.controls.AwbModeEnum.Auto,
            "AeEnable": True,
            "AeMeteringMode": libcamera.controls.AeMeteringModeEnum.CentreWeighted,
            "AeExposureMode": libcamera.controls.AeExposureModeEnum.Normal,
            "Sharpness": 1.5,
            "Contrast": 1.1,
            "Saturation": 1.2,
            "Brightness": 0.0,
            "NoiseReductionMode": libcamera.controls.draft.NoiseReductionModeEnum.Fast,
        },
        buffer_count=4,
    )
    cam.configure(config)
    cam.start()
    time.sleep(2)
    print("Camera started — 640x480 @ 30fps")


def load_model():
    global model
    # YOLOv8n — smallest/fastest YOLO, optimized for edge devices
    model = YOLO("yolov8n.pt")
    model.fuse()  # fuse Conv+BN layers for faster inference
    print("YOLOv8n loaded and fused.")


def draw_detections(frame, results):
    for result in results:
        for box in result.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            cls = int(box.cls[0])
            label = f"{model.names[cls]} {conf:.0%}"

            # Box
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # Label background
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 255, 0), -1)

            # Label text
            cv2.putText(frame, label, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    return frame


def capture_loop():
    global latest_frame
    while True:
        with cam_lock:
            frame = cam.capture_array()

        # Run YOLOv8n inference — conf 0.4, only CPU, half=False for Pi
        results = model.predict(frame, imgsz=320, conf=0.4, verbose=False, device="cpu")
        frame = draw_detections(frame, results)

        _, jpeg = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, 80, cv2.IMWRITE_JPEG_OPTIMIZE, 1]
        )
        with frame_lock:
            latest_frame = jpeg.tobytes()


def generate():
    while True:
        with frame_lock:
            frame = latest_frame
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        else:
            time.sleep(0.01)


@app.route("/")
def index():
    return """
    <html>
    <head>
        <title>Tony the Bot — Object Detection</title>
        <style>
            body { background: #111; display: flex; flex-direction: column;
                   align-items: center; justify-content: center; height: 100vh; margin: 0; }
            h2 { color: #0f0; font-family: monospace; margin-bottom: 12px; }
            img { border: 2px solid #0f0; border-radius: 4px; max-width: 100%; }
        </style>
    </head>
    <body>
        <h2>Tony the Bot — YOLOv8n Live Detection</h2>
        <img src="/video_feed">
    </body>
    </html>
    """


@app.route("/video_feed")
def video_feed():
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    load_model()
    start_camera()
    capture_thread = threading.Thread(target=capture_loop, daemon=True)
    capture_thread.start()
    print("Stream live at http://10.1.65.108:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True)
