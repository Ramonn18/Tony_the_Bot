/**
 * EmotionUI — auto-injects toggle button + emotion panel into the dashboard DOM.
 * Toggle button follows existing .hbtn pattern; panel inserts at top of .right-col.
 */
var EmotionUI = (function () {
  'use strict';

  var active     = false;
  var panelEl    = null;
  var toggleBtn  = null;
  var alertEl    = null;
  var bars       = {};
  var badgeEl    = null;
  var EMOTIONS   = ['confusion', 'happy', 'surprised', 'neutral'];
  var LABELS     = { confusion: 'CONFUSION', happy: 'HAPPY', surprised: 'SURPRISED', neutral: 'NEUTRAL' };
  var COLORS     = { confusion: 'var(--magenta)', happy: 'var(--yellow)', surprised: 'var(--orange)', neutral: 'var(--text2)' };

  function init() {
    injectToggleButton();
    injectPanel();
    registerKeyboard();
  }

  function injectToggleButton() {
    var topbar = document.querySelector('.topbar');
    if (!topbar) return;
    toggleBtn = document.createElement('button');
    toggleBtn.className = 'hbtn';
    toggleBtn.id = 'emotion-btn';
    toggleBtn.textContent = 'EMOTION';
    toggleBtn.onclick = toggle;
    // Insert before the snap-btn
    var snapBtn = document.getElementById('snap-btn');
    if (snapBtn) {
      topbar.insertBefore(toggleBtn, snapBtn);
    } else {
      topbar.appendChild(toggleBtn);
    }
  }

  function injectPanel() {
    var rightCol = document.querySelector('.right-col');
    if (!rightCol) return;

    panelEl = document.createElement('div');
    panelEl.className = 'emotion-panel';
    panelEl.id = 'emotion-panel';
    panelEl.style.display = 'none';

    // Alert banner (hidden by default)
    alertEl = document.createElement('div');
    alertEl.className = 'emotion-alert';
    alertEl.id = 'emotion-alert';
    alertEl.textContent = 'CONFUSION DETECTED';
    alertEl.style.display = 'none';
    panelEl.appendChild(alertEl);

    // Header row
    var header = document.createElement('div');
    header.className = 'emotion-header';
    header.innerHTML =
      '<span class="emotion-title">Emotion Detection</span>' +
      '<span class="emotion-badge" id="emotion-badge">OFF</span>';
    panelEl.appendChild(header);
    badgeEl = header.querySelector('#emotion-badge');

    // Bar list
    var barList = document.createElement('div');
    barList.className = 'emotion-bars';

    EMOTIONS.forEach(function (key) {
      var row = document.createElement('div');
      row.className = 'emotion-bar-row' + (key === 'confusion' ? ' confusion-row' : '');

      var label = document.createElement('span');
      label.className = 'emotion-label' + (key === 'confusion' ? ' confusion-label' : '');
      label.textContent = LABELS[key];

      var track = document.createElement('div');
      track.className = 'emotion-bar-track';

      var fill = document.createElement('div');
      fill.className = 'emotion-bar-fill';
      fill.style.background = COLORS[key];
      fill.style.width = '0%';
      track.appendChild(fill);

      var val = document.createElement('span');
      val.className = 'emotion-val';
      val.textContent = '0%';

      row.appendChild(label);
      row.appendChild(track);
      row.appendChild(val);
      barList.appendChild(row);

      bars[key] = { fill: fill, val: val };
    });

    panelEl.appendChild(barList);

    // Insert at top of right-col (before gauge-row)
    rightCol.insertBefore(panelEl, rightCol.firstChild);
  }

  function registerKeyboard() {
    document.addEventListener('keydown', function (e) {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.key.toLowerCase() === 'm') toggle();
    });
  }

  function toggle() {
    if (active) {
      deactivate();
    } else {
      activate();
    }
  }

  function activate() {
    active = true;
    panelEl.style.display = '';
    toggleBtn.classList.add('active');
    badgeEl.textContent = 'LOADING';
    badgeEl.className = 'emotion-badge warn';

    FaceMeshController.start(function (result) {
      updateBars(result);
    });

    // Update badge once model is running (check after a delay)
    setTimeout(function checkReady() {
      if (FaceMeshController.isRunning()) {
        badgeEl.textContent = 'LIVE';
        badgeEl.className = 'emotion-badge live';
      } else if (active) {
        setTimeout(checkReady, 500);
      }
    }, 1000);
  }

  function deactivate() {
    active = false;
    FaceMeshController.stop();
    panelEl.style.display = 'none';
    toggleBtn.classList.remove('active');
    alertEl.style.display = 'none';
    badgeEl.textContent = 'OFF';
    badgeEl.className = 'emotion-badge';

    // Reset bars
    EMOTIONS.forEach(function (key) {
      bars[key].fill.style.width = '0%';
      bars[key].val.textContent = '0%';
    });
  }

  function updateBars(result) {
    if (!result || !active) return;

    EMOTIONS.forEach(function (key) {
      var pct = result[key];
      bars[key].fill.style.width = pct + '%';
      bars[key].val.textContent = pct + '%';
    });

    // Confusion alert
    if (result.confusion > 50) {
      alertEl.style.display = '';
    } else {
      alertEl.style.display = 'none';
    }

    // Badge
    if (result.dominant === 'confusion') {
      badgeEl.textContent = 'CONFUSION';
      badgeEl.className = 'emotion-badge crit';
    } else {
      badgeEl.textContent = result.dominant.toUpperCase();
      badgeEl.className = 'emotion-badge live';
    }
  }

  // Auto-init when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { toggle: toggle };
})();
