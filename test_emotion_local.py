"""
Local test server for emotion detection — no Pi dependencies needed.
Run: python test_emotion_local.py
Open: http://localhost:8000
"""
from http.server import HTTPServer, SimpleHTTPRequestHandler
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>// EMOTION DETECTION TEST</title>
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
      color: var(--text);
      font-family: 'Inter', sans-serif;
      height: 100vh;
      display: flex; flex-direction: column;
      align-items: center;
      padding: 24px;
    }
    @keyframes blink { 50% { opacity: 0; } }

    .topbar {
      display: flex; align-items: center; gap: 10px;
      padding: 0 16px; height: 48px; width: 100%; max-width: 600px;
      background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
      margin-bottom: 16px;
    }
    .brand-title {
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.72rem; color: var(--yellow); letter-spacing: 3px;
    }
    .h-spacer { flex: 1; }
    .hbtn {
      background: transparent; border: 1px solid var(--borderb);
      color: var(--text2); font-family: 'Share Tech Mono', monospace;
      font-size: 0.56rem; letter-spacing: 1px; cursor: pointer;
      padding: 5px 10px; text-transform: uppercase; border-radius: 4px;
      transition: all 0.15s;
    }
    .hbtn:hover { color: var(--cyan); border-color: var(--cyan); }
    .hbtn.active { color: var(--yellow); border-color: var(--yellow); background: rgba(245,197,24,0.07); }

    .right-col {
      width: 100%; max-width: 600px;
      display: flex; flex-direction: column; gap: 12px;
    }

    .webcam-preview {
      width: 100%; max-width: 600px;
      background: var(--card); border: 1px solid var(--border); border-radius: 8px;
      overflow: hidden; display: none; position: relative;
    }
    .webcam-preview video {
      width: 100%; display: block;
    }
    .webcam-label {
      position: absolute; bottom: 8px; left: 8px;
      font-family: 'Share Tech Mono', monospace; font-size: 0.5rem;
      color: var(--green); letter-spacing: 2px;
      background: rgba(0,0,0,0.6); padding: 2px 8px; border-radius: 3px;
    }

    .instructions {
      background: var(--card); border: 1px solid var(--border); border-radius: 8px;
      padding: 16px; font-family: 'Share Tech Mono', monospace; font-size: 0.6rem;
      color: var(--text2); line-height: 1.8; letter-spacing: 0.5px;
    }
    .instructions span { color: var(--yellow); }
  </style>
  <link rel="stylesheet" href="/emotion_detection/emotion_styles.css">
  <script src="https://unpkg.com/ml5@1/dist/ml5.min.js"></script>
</head>
<body>

  <div class="topbar">
    <span class="brand-title">EMOTION_TEST</span>
    <div class="h-spacer"></div>
    <!-- EmotionUI will inject EMOTION button here -->
  </div>

  <div class="right-col">
    <!-- EmotionUI will inject panel here -->

    <div class="webcam-preview" id="webcam-preview">
      <video id="preview-video" autoplay playsinline muted></video>
      <span class="webcam-label">WEBCAM LIVE</span>
    </div>

    <div class="instructions">
      <p><span>1.</span> Click the <span>EMOTION</span> button above (or press <span>M</span>)</p>
      <p><span>2.</span> Allow webcam access when prompted</p>
      <p><span>3.</span> Try these expressions:</p>
      <p>&nbsp;&nbsp;&nbsp;<span>CONFUSION</span> — furrow brows, squint, tilt head</p>
      <p>&nbsp;&nbsp;&nbsp;<span>HAPPY</span> — smile wide</p>
      <p>&nbsp;&nbsp;&nbsp;<span>SURPRISED</span> — raise eyebrows, open mouth</p>
      <p>&nbsp;&nbsp;&nbsp;<span>SAD</span> — raise inner brows, frown</p>
      <p><span>4.</span> Press <span>M</span> again to toggle off</p>
    </div>
  </div>

  <script src="/emotion_detection/emotion_engine.js"></script>
  <script src="/emotion_detection/facemesh_controller.js"></script>
  <script src="/emotion_detection/emotion_ui.js"></script>
  <script>
    // Show a webcam preview so you can see yourself
    (function() {
      var origStart = FaceMeshController.start;
      var origStop  = FaceMeshController.stop;
      var previewEl = document.getElementById('webcam-preview');
      var previewVid = document.getElementById('preview-video');

      FaceMeshController.start = function(cb) {
        origStart(cb);
        // Mirror the webcam into the visible preview
        navigator.mediaDevices.getUserMedia({
          video: { width: 640, height: 480, facingMode: 'user' },
          audio: false
        }).then(function(s) {
          previewVid.srcObject = s;
          previewEl.style.display = 'block';
          previewEl._stream = s;
        }).catch(function(){});
      };

      FaceMeshController.stop = function() {
        origStop();
        if (previewEl._stream) {
          previewEl._stream.getTracks().forEach(function(t){ t.stop(); });
          previewEl._stream = null;
        }
        previewVid.srcObject = null;
        previewEl.style.display = 'none';
      };
    })();
  </script>
</body>
</html>
"""

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path.startswith('/emotion_detection/'):
            # Serve static files from emotion_detection/
            super().do_GET()
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        print(f"  {args[0]}")

if __name__ == '__main__':
    port = 8000
    server = HTTPServer(('localhost', port), Handler)
    print(f"Emotion detection test server running at:")
    print(f"  http://localhost:{port}")
    print(f"Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
