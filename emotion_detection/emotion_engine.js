/**
 * EmotionEngine — geometric emotion computation from FaceMesh 468 landmarks.
 * Uses FACS-inspired Action Unit distances / ratios.
 * No external ML model needed; pure landmark geometry.
 */
var EmotionEngine = (function () {
  'use strict';

  // Key landmark indices (MediaPipe FaceMesh 468-point topology)
  var LM = {
    // Brow landmarks
    LEFT_BROW_INNER:  107,
    LEFT_BROW_MID:    105,
    LEFT_BROW_OUTER:  70,
    RIGHT_BROW_INNER: 336,
    RIGHT_BROW_MID:   334,
    RIGHT_BROW_OUTER: 300,

    // Eye landmarks
    LEFT_EYE_TOP:     159,
    LEFT_EYE_BOTTOM:  145,
    LEFT_EYE_INNER:   133,
    LEFT_EYE_OUTER:   33,
    RIGHT_EYE_TOP:    386,
    RIGHT_EYE_BOTTOM: 374,
    RIGHT_EYE_INNER:  362,
    RIGHT_EYE_OUTER:  263,

    // Nose
    NOSE_TIP:         1,
    NOSE_BRIDGE:      6,

    // Mouth
    MOUTH_LEFT:       61,
    MOUTH_RIGHT:      291,
    MOUTH_TOP:        13,
    MOUTH_BOTTOM:     14,
    UPPER_LIP_TOP:    0,
    LOWER_LIP_BOTTOM: 17,

    // Face oval (for normalization)
    CHIN:             152,
    FOREHEAD:         10,

    // Head tilt reference
    LEFT_CHEEK:       234,
    RIGHT_CHEEK:      454,
  };

  function dist(a, b) {
    var dx = a.x - b.x, dy = a.y - b.y, dz = (a.z || 0) - (b.z || 0);
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  }

  function clamp01(v) { return Math.max(0, Math.min(1, v)); }
  function smooth(prev, cur, alpha) { return prev * (1 - alpha) + cur * alpha; }

  // Running smoothed values
  var prev = { confusion: 0, happy: 0, surprised: 0, neutral: 0 };
  var ALPHA = 0.35;

  /**
   * Analyze 468 keypoints and return emotion scores.
   * @param {Array} kp - array of {x, y, z} keypoints from FaceMesh
   * @returns {Object} {confusion, happy, surprised, neutral, dominant}
   */
  function analyze(kp) {
    if (!kp || kp.length < 468) return null;

    var p = function (idx) { return kp[idx]; };

    // Face height for normalization
    var faceH = dist(p(LM.FOREHEAD), p(LM.CHIN));
    if (faceH < 0.001) return null;
    var norm = function (d) { return d / faceH; };

    // ── Eye Aspect Ratio (EAR) ──
    var leftEAR  = dist(p(LM.LEFT_EYE_TOP), p(LM.LEFT_EYE_BOTTOM)) /
                   dist(p(LM.LEFT_EYE_INNER), p(LM.LEFT_EYE_OUTER));
    var rightEAR = dist(p(LM.RIGHT_EYE_TOP), p(LM.RIGHT_EYE_BOTTOM)) /
                   dist(p(LM.RIGHT_EYE_INNER), p(LM.RIGHT_EYE_OUTER));
    var avgEAR   = (leftEAR + rightEAR) / 2;

    // ── Brow positions (relative to eye) ──
    var leftBrowH  = norm(dist(p(LM.LEFT_BROW_MID), p(LM.LEFT_EYE_TOP)));
    var rightBrowH = norm(dist(p(LM.RIGHT_BROW_MID), p(LM.RIGHT_EYE_TOP)));
    var avgBrowH   = (leftBrowH + rightBrowH) / 2;

    // Inner brow distance (brow furrow = smaller distance)
    var innerBrowDist = norm(dist(p(LM.LEFT_BROW_INNER), p(LM.RIGHT_BROW_INNER)));

    // ── Mouth metrics ──
    var mouthW    = norm(dist(p(LM.MOUTH_LEFT), p(LM.MOUTH_RIGHT)));
    var mouthH    = norm(dist(p(LM.MOUTH_TOP), p(LM.MOUTH_BOTTOM)));
    var mouthOpen = mouthH / (mouthW + 0.001);

    // Mouth corner vertical position relative to mouth center
    var mouthCenterY = (p(LM.MOUTH_TOP).y + p(LM.MOUTH_BOTTOM).y) / 2;
    var mouthCornerY = (p(LM.MOUTH_LEFT).y + p(LM.MOUTH_RIGHT).y) / 2;
    var smileRatio   = (mouthCenterY - mouthCornerY) / faceH; // positive = smile

    // ── Head tilt (roll) ──
    var leftCheekY  = p(LM.LEFT_CHEEK).y;
    var rightCheekY = p(LM.RIGHT_CHEEK).y;
    var headTilt    = Math.abs(leftCheekY - rightCheekY) / faceH;

    // ═══════════════════════════════════════
    //  CONFUSION  (prioritized, boosted weights)
    //  AU4 (brow furrow) + AU7 (eye squint) + oblique brows + head tilt + lip compression
    // ═══════════════════════════════════════
    var browFurrow   = clamp01((0.18 - innerBrowDist) / 0.05);   // tighter threshold
    var eyeSquint    = clamp01((0.28 - avgEAR) / 0.08);          // tighter threshold
    var browLower    = clamp01((0.08 - avgBrowH) / 0.025);       // tighter threshold
    var tiltScore    = clamp01((headTilt - 0.012) / 0.035);      // more sensitive to tilt
    var lipCompress  = clamp01((0.02 - mouthOpen) / 0.012);

    // Asymmetric brows (one raised, one lowered) common in confusion
    var browAsymmetry = clamp01(Math.abs(leftBrowH - rightBrowH) / 0.02);

    // Frown component — downturned mouth contributes to confusion (was in sad)
    var frownScore    = clamp01((-smileRatio - 0.002) / 0.015);

    var confusion = clamp01(
      browFurrow    * 0.25 +
      eyeSquint     * 0.20 +
      browLower     * 0.10 +
      tiltScore     * 0.15 +
      lipCompress   * 0.05 +
      browAsymmetry * 0.15 +
      frownScore    * 0.10
    );

    // ═══════════════════════════════════════
    //  HAPPY — smile (mouth corners up, wide mouth)
    // ═══════════════════════════════════════
    var smileScore   = clamp01((smileRatio - 0.005) / 0.025);
    var mouthWide    = clamp01((mouthW - 0.28) / 0.08);
    var happy = clamp01(smileScore * 0.65 + mouthWide * 0.35);

    // ═══════════════════════════════════════
    //  SURPRISED — wide eyes, raised brows, open mouth
    // ═══════════════════════════════════════
    var wideEyes      = clamp01((avgEAR - 0.32) / 0.10);
    var highBrows     = clamp01((avgBrowH - 0.10) / 0.04);
    var mouthWideOpen = clamp01((mouthOpen - 0.35) / 0.25);
    var surprised     = clamp01(wideEyes * 0.35 + highBrows * 0.35 + mouthWideOpen * 0.30);

    // ═══════════════════════════════════════
    //  NEUTRAL — inverse of all others
    // ═══════════════════════════════════════
    var maxEmotion = Math.max(confusion, happy, surprised);
    var neutral    = clamp01(1 - maxEmotion * 1.5);

    // Smooth values
    confusion = smooth(prev.confusion, confusion, ALPHA);
    happy     = smooth(prev.happy, happy, ALPHA);
    surprised = smooth(prev.surprised, surprised, ALPHA);
    neutral   = smooth(prev.neutral, neutral, ALPHA);

    prev.confusion = confusion;
    prev.happy     = happy;
    prev.surprised = surprised;
    prev.neutral   = neutral;

    // Determine dominant
    var emotions = [
      { name: 'confusion', score: confusion },
      { name: 'happy',     score: happy },
      { name: 'surprised', score: surprised },
      { name: 'neutral',   score: neutral },
    ];
    emotions.sort(function (a, b) { return b.score - a.score; });

    return {
      confusion:  Math.round(confusion * 100),
      happy:      Math.round(happy * 100),
      surprised:  Math.round(surprised * 100),
      neutral:    Math.round(neutral * 100),
      dominant:   emotions[0].name,
    };
  }

  function reset() {
    prev = { confusion: 0, happy: 0, surprised: 0, neutral: 0 };
  }

  return { analyze: analyze, reset: reset };
})();
