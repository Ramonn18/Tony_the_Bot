#!/usr/bin/env python3
"""patch_grid_layout.py — Convert fixed-position panels to CSS Grid layout."""

TARGET = "/home/tony1/Tony_the_Bot/stream_server.py"

with open(TARGET, "r", encoding="utf-8") as f:
    src = f.read()

original = src

# ── 1. Replace .main CSS: flex → grid with 3 columns ──────────────────────
old_main_css = "    /* ── MAIN LAYOUT ── */\n    .main { display: flex; flex: 1; overflow: hidden; }"
new_main_css = (
    "    /* ── MAIN LAYOUT (CSS GRID) ── */\n"
    "    .main {\n"
    "      display: grid;\n"
    "      grid-template-columns: 256px 1fr 300px;\n"
    "      grid-template-rows: 1fr;\n"
    "      flex: 1;\n"
    "      gap: 12px;\n"
    "      padding: 12px;\n"
    "      overflow: hidden;\n"
    "      min-height: 0;\n"
    "    }"
)
assert old_main_css in src, "FAIL: .main CSS not found"
src = src.replace(old_main_css, new_main_css, 1)

# ── 2. Strip position:fixed from .detection-float ─────────────────────────
old_det_css = (
    "    /* ── DETECTION FLOATING PANEL ── */\n"
    "    .detection-float {\n"
    "      position: fixed;\n"
    "      right: 16px;\n"
    "      top: 66px;\n"
    "      width: 300px;\n"
    "      height: calc(100vh - 82px);\n"
    "      background: rgba(8, 8, 16, 0.82);\n"
    "      backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);\n"
    "      border: 1px solid var(--border); border-radius: 14px;\n"
    "      box-shadow: 0 6px 32px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.03);\n"
    "      overflow: hidden; display: flex; flex-direction: column; z-index: 100;\n"
    "    }"
)
new_det_css = (
    "    /* ── DETECTION FLOATING PANEL ── */\n"
    "    .detection-float {\n"
    "      background: rgba(8, 8, 16, 0.82);\n"
    "      backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);\n"
    "      border: 1px solid var(--border); border-radius: 14px;\n"
    "      box-shadow: 0 6px 32px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.03);\n"
    "      overflow: hidden; display: flex; flex-direction: column;\n"
    "      min-height: 0;\n"
    "    }"
)
assert old_det_css in src, "FAIL: .detection-float CSS not found"
src = src.replace(old_det_css, new_det_css, 1)

# ── 3. Strip position:fixed from .left-panels ─────────────────────────────
old_left_css = (
    "    /* ── LEFT FLOATING PANELS ── */\n"
    "    .left-panels {\n"
    "      position: fixed;\n"
    "      left: 16px;\n"
    "      top: 66px;\n"
    "      display: flex;\n"
    "      flex-direction: column;\n"
    "      gap: 10px;\n"
    "      z-index: 100;\n"
    "      max-height: calc(100vh - 82px);\n"
    "      overflow-y: auto;\n"
    "      scrollbar-width: none;\n"
    "    }"
)
new_left_css = (
    "    /* ── LEFT PANELS (grid column) ── */\n"
    "    .left-panels {\n"
    "      display: flex;\n"
    "      flex-direction: column;\n"
    "      gap: 10px;\n"
    "      overflow-y: auto;\n"
    "      scrollbar-width: none;\n"
    "      min-height: 0;\n"
    "      align-self: start;\n"
    "      max-height: 100%;\n"
    "    }"
)
assert old_left_css in src, "FAIL: .left-panels CSS not found"
src = src.replace(old_left_css, new_left_css, 1)

# ── 4. Add .video-panel grid centering CSS ─────────────────────────────────
old_vid_css = (
    "    /* ── VIDEO PANEL ── */\n"
    "    .video-panel {\n"
    "      flex: 1; display: flex; align-items: center; justify-content: center;\n"
    "      padding: 16px; background: var(--bg);\n"
    "    }"
)
new_vid_css = (
    "    /* ── VIDEO PANEL ── */\n"
    "    .video-panel {\n"
    "      display: flex; align-items: center; justify-content: center;\n"
    "      min-width: 0; min-height: 0;\n"
    "    }"
)
assert old_vid_css in src, "FAIL: .video-panel CSS not found"
src = src.replace(old_vid_css, new_vid_css, 1)

# ── 5. HTML: move .left-panels INSIDE .main, before video-panel ───────────
# Currently: </div><!-- /left-panels --> \n\n  <div class="main">
old_html_wrap = (
    "  </div><!-- /left-panels -->\n"
    "\n"
    "  <div class=\"main\">\n"
    "    <!-- ── VIDEO ── -->"
)
new_html_wrap = (
    "  </div><!-- /left-panels -->\n"
    "\n"
    "    <!-- ── VIDEO ── -->"
)
assert old_html_wrap in src, "FAIL: HTML wrap anchor not found"
src = src.replace(old_html_wrap, new_html_wrap, 1)

# Move the opening <div class="left-panels"> to inside <div class="main">
old_main_open = "  <!-- ── LEFT FLOATING PANELS ── -->\n  <div class=\"left-panels\">"
new_main_open = "  <div class=\"main\">\n\n  <!-- ── LEFT FLOATING PANELS ── -->\n  <div class=\"left-panels\">"
assert old_main_open in src, "FAIL: left-panels HTML open not found"
src = src.replace(old_main_open, new_main_open, 1)

# ── 6. HTML: move .detection-float inside .main, replace </div><!-- /main -->
old_main_close = (
    "  </div><!-- /main -->\n"
    "\n"
    "  <!-- ── DETECTION FLOATING PANEL ── -->\n"
    "  <div class=\"detection-float\">"
)
new_main_close = (
    "\n"
    "  <!-- ── DETECTION FLOATING PANEL ── -->\n"
    "  <div class=\"detection-float\">"
)
assert old_main_close in src, "FAIL: /main + detection-float HTML not found"
src = src.replace(old_main_close, new_main_close, 1)

# Close the detection-float div AND the main grid div
old_det_close = (
    "    </div>\n"
    "  </div>\n"
    "\n"
    "  <script>"
)
new_det_close = (
    "    </div>\n"
    "  </div><!-- /detection-float -->\n"
    "\n"
    "  </div><!-- /main grid -->\n"
    "\n"
    "  <script>"
)
assert old_det_close in src, "FAIL: detection-float close + script not found"
src = src.replace(old_det_close, new_det_close, 1)

# ── Verify nothing is identical ───────────────────────────────────────────
assert src != original, "FAIL: No changes made"

with open(TARGET, "w", encoding="utf-8") as f:
    f.write(src)

print("OK: Grid layout applied successfully")
