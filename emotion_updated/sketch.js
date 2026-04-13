let faceMesh;
let handPose;
let video;

let faces = [];
let hands = [];

let systemState = "idle"; // idle | preparing | detecting | paused
let showDebug = false;

let eyebrowScore = 0;
let headScore = 0;
let handScore = 0;
let rawConfusionScore = 0;
let smoothedConfusionScore = 0;
let emotionState = "Neutral";

const faceOptions = {
  maxFaces: 1,
  refineLandmarks: false,
  flipHorizontal: true
};

const handOptions = {
  maxHands: 2,
  flipHorizontal: true
};

// ---------- DOM refs ----------
let startBtn;
let stopBtn;
let resetBtn;
let systemPill;
let emotionStateEl;
let confidenceTextEl;
let confidenceFillEl;
let eyebrowValueEl;
let headValueEl;
let handValueEl;
let statusNoteEl;

// ---------- p5 preload ----------
function preload() {
  faceMesh = ml5.faceMesh(faceOptions);
  handPose = ml5.handPose(handOptions);
}

// ---------- setup ----------
function setup() {
  const cnv = createCanvas(960, 640);
  cnv.parent("canvas-wrapper");

  video = createCapture(VIDEO);
  video.size(960, 640);
  video.hide();

  cacheDOM();
  bindUI();
  updateUI();
}

// ---------- draw ----------
function draw() {
  background(235);

  drawVideoMirrored();

  if (systemState === "idle") {
    drawCenterMessage("Press Start Detection");
    return;
  }

  if (systemState === "preparing") {
    drawPreparationOverlay();
    evaluateReadiness();
    updateScoresAndState();
    updateUI();
    return;
  }

  if (systemState === "paused") {
    drawPausedOverlay();
    updateUI();
    return;
  }

  if (systemState === "detecting") {
    updateScoresAndState();
    drawDetectionOverlay();
    updateUI();
  }
}

// ---------- UI ----------
function cacheDOM() {
  startBtn = document.getElementById("startBtn");
  stopBtn = document.getElementById("stopBtn");
  resetBtn = document.getElementById("resetBtn");

  systemPill = document.getElementById("system-pill");
  emotionStateEl = document.getElementById("emotionState");
  confidenceTextEl = document.getElementById("confidenceText");
  confidenceFillEl = document.getElementById("confidenceFill");

  eyebrowValueEl = document.getElementById("eyebrowValue");
  headValueEl = document.getElementById("headValue");
  handValueEl = document.getElementById("handValue");
  statusNoteEl = document.getElementById("statusNote");
}

function bindUI() {
  startBtn.addEventListener("click", startDetection);
  stopBtn.addEventListener("click", stopDetection);
  resetBtn.addEventListener("click", resetDetection);
}

function updateUI() {
  systemPill.textContent = capitalize(systemState);
  systemPill.className = `pill ${systemState}`;

  emotionStateEl.textContent = emotionState;
  emotionStateEl.className = "state " + getEmotionClass(emotionState);

  confidenceTextEl.textContent = `${Math.round(smoothedConfusionScore * 100)}%`;
  confidenceFillEl.style.width = `${Math.round(smoothedConfusionScore * 100)}%`;

  eyebrowValueEl.textContent = eyebrowScore.toFixed(2);
  headValueEl.textContent = headScore.toFixed(2);
  handValueEl.textContent = handScore.toFixed(2);

  if (systemState === "idle") {
    statusNoteEl.textContent =
      "Press Start to begin real-time confusion detection.";
  } else if (systemState === "preparing") {
    statusNoteEl.textContent =
      "Checking if your face and hand are visible. Please face the camera.";
  } else if (systemState === "paused") {
    statusNoteEl.textContent =
      "Detection paused. Press Start to resume live sensing.";
  } else {
    statusNoteEl.textContent =
      "Analyzing eyebrow tension, head direction, and hand proximity.";
  }
}

function getEmotionClass(state) {
  if (state === "Confused") return "confused";
  if (state === "Possible confusion") return "possible";
  return "neutral";
}

// ---------- interaction ----------
function startDetection() {
  if (systemState === "detecting" || systemState === "preparing") return;

  systemState = "preparing";

  faceMesh.detectStart(video, gotFaces);
  handPose.detectStart(video, gotHands);
}

function stopDetection() {
  if (systemState === "idle") return;

  systemState = "paused";

  try {
    faceMesh.detectStop();
  } catch (e) {}

  try {
    handPose.detectStop();
  } catch (e) {}
}

function resetDetection() {
  stopDetection();

  faces = [];
  hands = [];

  eyebrowScore = 0;
  headScore = 0;
  handScore = 0;
  rawConfusionScore = 0;
  smoothedConfusionScore = 0;
  emotionState = "Neutral";

  systemState = "idle";
  updateUI();
}

// ---------- callbacks ----------
function gotFaces(results) {
  faces = results || [];
}

function gotHands(results) {
  hands = results || [];
}

// ---------- detection logic ----------
function evaluateReadiness() {
  const hasFace = faces.length > 0;
  const hasHand = hands.length > 0;

  if (hasFace && hasHand) {
    systemState = "detecting";
  }
}

function updateScoresAndState() {
  if (faces.length === 0) {
    eyebrowScore = 0;
    headScore = 0;
    handScore = 0;
    rawConfusionScore = 0;
    smoothedConfusionScore = lerp(smoothedConfusionScore, 0, 0.08);
    emotionState = "Neutral";
    return;
  }

  const face = faces[0];

  eyebrowScore = estimateEyebrowScore(face);
  headScore = estimateHeadDirectionScore(face);
  handScore = estimateHandNearFaceScore(face, hands);

  rawConfusionScore =
    eyebrowScore * 0.4 +
    headScore * 0.35 +
    handScore * 0.25;

  rawConfusionScore = constrain(rawConfusionScore, 0, 1);
  smoothedConfusionScore = lerp(smoothedConfusionScore, rawConfusionScore, 0.12);

  if (smoothedConfusionScore > 0.68) {
    emotionState = "Confused";
  } else if (smoothedConfusionScore > 0.4) {
    emotionState = "Possible confusion";
  } else {
    emotionState = "Neutral";
  }
}

// ---------- heuristics ----------
function estimateEyebrowScore(face) {
  if (!face.keypoints || face.keypoints.length < 468) return 0;

  // approximate eyebrow + eye landmarks
  const leftBrow = avgPoint(face, [70, 63, 105, 66, 107]);
  const rightBrow = avgPoint(face, [336, 296, 334, 293, 300]);

  const leftEyeTop = getPoint(face, 159);
  const leftEyeBottom = getPoint(face, 145);
  const rightEyeTop = getPoint(face, 386);
  const rightEyeBottom = getPoint(face, 374);

  if (!leftBrow || !rightBrow || !leftEyeTop || !leftEyeBottom || !rightEyeTop || !rightEyeBottom) {
    return 0;
  }

  const leftEyeCenter = midpoint(leftEyeTop, leftEyeBottom);
  const rightEyeCenter = midpoint(rightEyeTop, rightEyeBottom);

  const leftBrowLift = dist(leftBrow.x, leftBrow.y, leftEyeCenter.x, leftEyeCenter.y);
  const rightBrowLift = dist(rightBrow.x, rightBrow.y, rightEyeCenter.x, rightEyeCenter.y);

  const eyeGapLeft = dist(leftEyeTop.x, leftEyeTop.y, leftEyeBottom.x, leftEyeBottom.y);
  const eyeGapRight = dist(rightEyeTop.x, rightEyeTop.y, rightEyeBottom.x, rightEyeBottom.y);
  const eyeGapAvg = (eyeGapLeft + eyeGapRight) / 2;

  if (eyeGapAvg < 1) return 0;

  // asymmetry + lift relative to eye opening
  const liftNorm = ((leftBrowLift + rightBrowLift) / 2) / eyeGapAvg;
  const asymmetry = abs(leftBrowLift - rightBrowLift) / eyeGapAvg;

  // tuned gently for a prototype
  const liftScore = mapClamp(liftNorm, 3.6, 5.8, 0, 1);
  const asymmetryScore = mapClamp(asymmetry, 0.15, 1.1, 0, 1);

  return constrain(liftScore * 0.6 + asymmetryScore * 0.4, 0, 1);
}

function estimateHeadDirectionScore(face) {
  if (!face.keypoints || face.keypoints.length < 468) return 0;

  const leftFace = getPoint(face, 234);
  const rightFace = getPoint(face, 454);
  const nose = getPoint(face, 1);
  const leftEyeOuter = getPoint(face, 33);
  const rightEyeOuter = getPoint(face, 263);

  if (!leftFace || !rightFace || !nose || !leftEyeOuter || !rightEyeOuter) {
    return 0;
  }

  const faceCenter = midpoint(leftFace, rightFace);

  const horizontalOffset = abs(nose.x - faceCenter.x);
  const faceWidth = dist(leftFace.x, leftFace.y, rightFace.x, rightFace.y);

  const eyeLineDy = abs(leftEyeOuter.y - rightEyeOuter.y);

  if (faceWidth < 1) return 0;

  const yawScore = mapClamp(horizontalOffset / faceWidth, 0.02, 0.12, 0, 1);
  const tiltScore = mapClamp(eyeLineDy / faceWidth, 0.01, 0.08, 0, 1);

  return constrain(yawScore * 0.6 + tiltScore * 0.4, 0, 1);
}

function estimateHandNearFaceScore(face, handsArr) {
  if (!handsArr || handsArr.length === 0) return 0;
  if (!face.keypoints || face.keypoints.length < 468) return 0;

  const leftFace = getPoint(face, 234);
  const rightFace = getPoint(face, 454);
  const nose = getPoint(face, 1);

  if (!leftFace || !rightFace || !nose) return 0;

  const faceWidth = dist(leftFace.x, leftFace.y, rightFace.x, rightFace.y);
  if (faceWidth < 1) return 0;

  let minDistance = Infinity;

  for (const hand of handsArr) {
    if (!hand.keypoints) continue;
    for (const kp of hand.keypoints) {
      const d = dist(kp.x, kp.y, nose.x, nose.y);
      if (d < minDistance) minDistance = d;
    }
  }

  const normalized = minDistance / faceWidth;

  // closer hand to face = higher score
  return 1 - mapClamp(normalized, 0.35, 1.5, 0, 1);
}

// ---------- drawing ----------
function drawVideoMirrored() {
  push();
  translate(width, 0);
  scale(-1, 1);
  image(video, 0, 0, width, height);
  pop();
}

function drawCenterMessage(msg) {
  push();
  fill(17, 24, 39, 180);
  noStroke();
  rectMode(CENTER);
  rect(width / 2, height / 2, 320, 80, 18);

  fill(255);
  textAlign(CENTER, CENTER);
  textSize(24);
  text(msg, width / 2, height / 2);
  pop();
}

function drawPreparationOverlay() {
  drawStatusBadge("Preparing");
  drawGuides();

  const faceReady = faces.length > 0;
  const handReady = hands.length > 0;

  const lines = [
    `Face: ${faceReady ? "detected" : "not detected"}`,
    `Hand: ${handReady ? "detected" : "not detected"}`
  ];

  push();
  fill(255, 250);
  noStroke();
  rect(24, 90, 260, 84, 16);

  fill(17);
  textSize(16);
  textAlign(LEFT, TOP);
  text("Readiness Check", 40, 106);

  fill(90);
  textSize(14);
  text(lines[0], 40, 132);
  text(lines[1], 40, 152);
  pop();

  if (showDebug) {
    drawFacePoints();
    drawHandPoints();
  }
}

function drawPausedOverlay() {
  drawStatusBadge("Paused");

  push();
  fill(17, 24, 39, 190);
  noStroke();
  rectMode(CENTER);
  rect(width / 2, height / 2, 260, 72, 16);

  fill(255);
  textAlign(CENTER, CENTER);
  textSize(22);
  text("Detection Paused", width / 2, height / 2);
  pop();

  if (showDebug) {
    drawFacePoints();
    drawHandPoints();
  }
}

function drawDetectionOverlay() {
  drawStatusBadge("Detecting");
  drawGuides();

  if (showDebug) {
    drawFacePoints();
    drawHandPoints();
    drawFeatureMarkers();
  }
}

function drawStatusBadge(label) {
  push();
  noStroke();
  fill(255, 248);
  rect(24, 24, 150, 44, 999);

  fill(17);
  textAlign(LEFT, CENTER);
  textSize(16);
  text(label, 42, 46);
  pop();
}

function drawGuides() {
  push();
  noFill();
  stroke(255, 255, 255, 120);
  strokeWeight(2);
  rect(120, 40, width - 240, height - 80, 28);
  pop();
}

function drawFacePoints() {
  if (faces.length === 0) return;
  const face = faces[0];

  push();
  noStroke();
  fill(60, 60, 60, 130);

  for (const kp of face.keypoints) {
    circle(width - kp.x, kp.y, 4);
  }
  pop();
}

function drawHandPoints() {
  if (hands.length === 0) return;

  push();
  noStroke();
  fill(20, 20, 20, 150);

  for (const hand of hands) {
    if (!hand.keypoints) continue;
    for (const kp of hand.keypoints) {
      circle(width - kp.x, kp.y, 8);
    }
  }
  pop();
}

function drawFeatureMarkers() {
  if (faces.length === 0) return;
  const face = faces[0];

  const points = [
    avgPoint(face, [70, 63, 105, 66, 107]),
    avgPoint(face, [336, 296, 334, 293, 300]),
    getPoint(face, 1),
    getPoint(face, 234),
    getPoint(face, 454),
    getPoint(face, 33),
    getPoint(face, 263)
  ].filter(Boolean);

  push();
  fill(255);
  noStroke();
  for (const p of points) {
    circle(width - p.x, p.y, 10);
  }
  pop();
}

// ---------- helpers ----------
function getPoint(face, index) {
  return face.keypoints && face.keypoints[index] ? face.keypoints[index] : null;
}

function avgPoint(face, indices) {
  let sumX = 0;
  let sumY = 0;
  let count = 0;

  for (const i of indices) {
    const p = getPoint(face, i);
    if (!p) continue;
    sumX += p.x;
    sumY += p.y;
    count++;
  }

  if (count === 0) return null;
  return { x: sumX / count, y: sumY / count };
}

function midpoint(a, b) {
  return {
    x: (a.x + b.x) / 2,
    y: (a.y + b.y) / 2
  };
}

function mapClamp(value, inMin, inMax, outMin, outMax) {
  const mapped = map(value, inMin, inMax, outMin, outMax);
  return constrain(mapped, Math.min(outMin, outMax), Math.max(outMin, outMax));
}

function capitalize(str) {
  return str.charAt(0).toUpperCase() + str.slice(1);
}

function keyPressed() {
  if (key === "d" || key === "D") {
    showDebug = !showDebug;
  }
}