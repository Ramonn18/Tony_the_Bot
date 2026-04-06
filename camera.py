from picamera2 import Picamera2
import time

def connect_camera():
    cam = Picamera2()
    config = cam.create_preview_configuration(main={"size": (640, 480), "format": "RGB888"})
    cam.configure(config)
    cam.start()
    print("Camera connected — OV5647 on Tony (Hexapod)")
    return cam

def capture_frame(cam):
    frame = cam.capture_array()
    return frame

def disconnect_camera(cam):
    cam.stop()
    cam.close()
    print("Camera disconnected.")

if __name__ == "__main__":
    cam = connect_camera()
    time.sleep(1)  # warm-up

    frame = capture_frame(cam)
    print(f"Frame captured: shape={frame.shape}, dtype={frame.dtype}")

    # Save a test snapshot
    cam.capture_file("snapshot.jpg")
    print("Snapshot saved to snapshot.jpg")

    disconnect_camera(cam)
