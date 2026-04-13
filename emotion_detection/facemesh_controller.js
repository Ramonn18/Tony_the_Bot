/**
 * FaceMeshController — manages ml5.js FaceMesh lifecycle, webcam, and detection loop.
 * Runs at 320x240 for performance. No p5.js dependency.
 * Uses ml5.js v1 API: await ml5.faceMesh() + detectStart(video, callback)
 */
var FaceMeshController = (function () {
  'use strict';

  var video    = null;
  var faceMesh = null;
  var running  = false;
  var stream   = null;
  var onResult = null;  // callback(emotionResult)

  function createVideo() {
    var v = document.createElement('video');
    v.setAttribute('playsinline', '');
    v.setAttribute('autoplay', '');
    v.width  = 320;
    v.height = 240;
    v.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;pointer-events:none;opacity:0;';
    document.body.appendChild(v);
    return v;
  }

  function startWebcam() {
    return navigator.mediaDevices.getUserMedia({
      video: { width: 320, height: 240, facingMode: 'user' },
      audio: false,
    }).then(function (s) {
      stream = s;
      video.srcObject = s;
      return new Promise(function (resolve) {
        video.onloadeddata = function () { resolve(); };
      });
    });
  }

  function stopWebcam() {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    if (video) {
      video.srcObject = null;
      video.remove();
      video = null;
    }
  }

  function gotResults(results) {
    if (!running) return;
    if (results && results.length > 0 && results[0].keypoints) {
      var kp = results[0].keypoints;
      var emotions = EmotionEngine.analyze(kp);
      if (emotions && onResult) onResult(emotions);
    }
  }

  /**
   * Start the FaceMesh detection pipeline.
   * @param {Function} callback - called with emotion result object each frame
   */
  function start(callback) {
    if (running) return;
    onResult = callback;
    video = createVideo();

    startWebcam().then(function () {
      // ml5.js v1: async constructor, then detectStart for continuous loop
      return ml5.faceMesh({
        maxFaces: 1,
        refineLandmarks: false,
        flipped: true,
      });
    }).then(function (model) {
      faceMesh = model;
      running = true;
      console.log('[FaceMesh] Model loaded, starting detection');
      faceMesh.detectStart(video, gotResults);
    }).catch(function (err) {
      console.error('[FaceMesh] Error:', err);
      stopWebcam();
    });
  }

  /**
   * Stop detection and release webcam.
   */
  function stop() {
    running  = false;
    onResult = null;
    if (faceMesh) {
      try { faceMesh.detectStop(); } catch (e) {}
      faceMesh = null;
    }
    stopWebcam();
    EmotionEngine.reset();
  }

  function isRunning() { return running; }

  return { start: start, stop: stop, isRunning: isRunning };
})();
