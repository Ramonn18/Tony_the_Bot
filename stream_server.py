from flask import Flask, Response
from picamera2 import Picamera2
import cv2
import threading

app = Flask(__name__)

cam = None
cam_lock = threading.Lock()
latest_frame = None
frame_lock = threading.Lock()


def start_camera():
    global cam
    cam = Picamera2()
    config = cam.create_preview_configuration(main={"size": (640, 480), "format": "RGB888"})
    cam.configure(config)
    cam.start()
    print("Camera started.")


def capture_loop():
    global latest_frame
    while True:
        with cam_lock:
            frame_rgb = cam.capture_array()
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        _, jpeg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
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


@app.route("/")
def index():
    return """
    <html>
    <head>
        <title>Tony the Bot — Live Camera</title>
        <style>
            body { background: #111; display: flex; flex-direction: column;
                   align-items: center; justify-content: center; height: 100vh; margin: 0; }
            h2 { color: #0f0; font-family: monospace; }
            img { border: 2px solid #0f0; border-radius: 4px; }
        </style>
    </head>
    <body>
        <h2>Tony the Bot — Live Feed</h2>
        <img src="/video_feed" width="640" height="480">
    </body>
    </html>
    """


@app.route("/video_feed")
def video_feed():
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    start_camera()
    capture_thread = threading.Thread(target=capture_loop, daemon=True)
    capture_thread.start()
    print("Stream live at http://10.1.65.108:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True)
