#!/usr/bin/env python3
"""
tony_creature.py — Tony as a stationary creature automaton.

Runs alongside tony_brain.py (camera/YOLO).
Drives leg animations + head pan/tilt based on state.

States (written by Discord bot to /tmp/tony_cmd.json):
  idle | loading | responding | greeting | flinch
  wave_once | wiggle | stretch | excited_once

Head tracking: reads /tmp/tony_state.json (person cx/cy from tony_brain.py).
"""
import json, math, os, threading, time
from adafruit_servokit import ServoKit

MAP_FILE    = os.path.join(os.path.dirname(__file__), "servo_map.json")
POSES_FILE  = os.path.join(os.path.dirname(__file__), "poses.json")
BRAIN_STATE = "/tmp/tony_state.json"
CMD_FILE    = "/tmp/tony_cmd.json"
PULSE       = (500, 2500)

TICK  = 0.02  # 50 Hz main loop
SPEED = 4     # max degrees moved per tick toward target

# ── Servo layer ───────────────────────────────────────────────────────────────
with open(MAP_FILE) as f:
    _servo_map = json.load(f)
with open(POSES_FILE) as f:
    _poses = json.load(f)

_leg_lookup  = {}
_head_lookup = {}
for v in _servo_map.values():
    if v.get("part") == "head":
        _head_lookup[v["joint"]] = (v["board"], v["channel"])
    elif v.get("leg") is not None:
        leg = v["leg"]
        _leg_lookup.setdefault(leg, {})[v["joint"]] = (v["board"], v["channel"])

_kits = {}
def _get_kit(board_str):
    addr = int(board_str, 16)
    if addr not in _kits:
        kit = ServoKit(channels=16, address=addr)
        for ch in range(16):
            kit.servo[ch].set_pulse_width_range(*PULSE)
        _kits[addr] = kit
    return _kits[addr]

def set_servo(leg, joint, angle):
    angle = max(0, min(180, int(round(angle))))
    board, ch = _leg_lookup[leg][joint]
    _get_kit(board).servo[ch].angle = angle

def set_head(joint, angle):
    angle = max(0, min(180, int(round(angle))))
    if joint not in _head_lookup:
        return
    board, ch = _head_lookup[joint]
    _get_kit(board).servo[ch].angle = angle

# ── Current angle tracking ────────────────────────────────────────────────────
HOME = {
    int(leg): {j: float(a) for j, a in joints.items()}
    for leg, joints in _poses["tony_flat"]["legs"].items()
}

_cur      = {leg: {j: float(HOME[leg][j]) for j in HOME[leg]} for leg in HOME}
_head_cur = {"pan": 90.0, "tilt": 90.0}

def _toward(current, target, speed):
    d = target - current
    return target if abs(d) <= speed else current + speed * (1.0 if d > 0 else -1.0)

def apply_frame(targets, pan_t, tilt_t, head_speed=1.5):
    for leg in range(1, 7):
        for j in ("coxa", "femur", "tibia"):
            _cur[leg][j] = _toward(_cur[leg][j], targets[leg][j], SPEED)
            set_servo(leg, j, _cur[leg][j])
    _head_cur["pan"]  = _toward(_head_cur["pan"],  pan_t,  head_speed)
    _head_cur["tilt"] = _toward(_head_cur["tilt"], tilt_t, head_speed)
    set_head("pan",  _head_cur["pan"])
    set_head("tilt", _head_cur["tilt"])

def home_targets():
    return {leg: {j: float(HOME[leg][j]) for j in HOME[leg]} for leg in HOME}

# ── Animation generators ──────────────────────────────────────────────────────
# Each yields (leg_targets, pan_target, tilt_target).
# Looping animations yield forever; one-shots raise StopIteration naturally.

def _return_home(ticks=25):
    for _ in range(ticks):
        yield home_targets(), 90.0, 90.0

def anim_breathe():
    t0 = time.time()
    while True:
        t  = time.time() - t0
        tg = home_targets()
        w  = math.sin(t * math.pi * 0.5) * 10  # 0.25 Hz, ±10°
        for leg in range(1, 7):
            tg[leg]["femur"] += w
        yield tg, 90.0, 90.0

def anim_wave_loop():
    """Continuous ripple legs 1→6, used for loading state."""
    PERIOD  = 1.4
    STAGGER = PERIOD / 6
    RAISE   = 28
    SWING   = 18
    t0 = time.time()
    while True:
        t  = time.time() - t0
        tg = home_targets()
        for i, leg in enumerate([1, 2, 3, 4, 5, 6]):
            phase = ((t - i * STAGGER) % PERIOD) / PERIOD
            s = math.sin(phase * math.pi * 2) if phase < 0.5 else 0.0
            tg[leg]["femur"] -= RAISE * s
            tg[leg]["coxa"]  += SWING * s * (1 if leg % 2 == 0 else -1)
        pan = 90.0 + 30.0 * math.sin(t * math.pi * 0.8)
        yield tg, pan, 90.0

def anim_excited_loop():
    """Fast alternating leg raises, used for responding state."""
    PERIOD = 0.5
    RAISE  = 20
    t0 = time.time()
    while True:
        t  = time.time() - t0
        tg = home_targets()
        for leg in range(1, 7):
            off   = (leg - 1) * PERIOD / 6
            phase = ((t - off) % PERIOD) / PERIOD
            s = math.sin(phase * math.pi * 2) if phase < 0.5 else 0.0
            tg[leg]["femur"] -= RAISE * s
        yield tg, 90.0, 85.0

def anim_greeting():
    for s in range(40):
        t  = s / 40
        tg = home_targets()
        ex = math.sin(t * math.pi) * 25
        for leg in range(1, 7):
            tg[leg]["coxa"]  += ex * (1 if leg % 2 == 0 else -1)
            tg[leg]["femur"] -= ex * 0.4
        yield tg, 90.0, 90.0
    for s in range(30):
        t  = s / 30
        tg = home_targets()
        w  = math.sin(t * math.pi * 6) * 12
        for leg in range(1, 7):
            tg[leg]["coxa"] += w * (1 if leg % 2 == 0 else -1)
        yield tg, 90.0, 90.0
    yield from _return_home()

def anim_flinch():
    for s in range(10):
        t  = (s + 1) / 10
        tg = home_targets()
        for leg in range(1, 7):
            tg[leg]["coxa"]  += (90 - HOME[leg]["coxa"])  * t * 0.6
            tg[leg]["femur"] += (90 - HOME[leg]["femur"]) * t * 0.4
        yield tg, 90.0, 80.0
    for _ in range(15):
        yield home_targets(), 90.0, 80.0
    yield from _return_home()

def anim_wave_once():
    gen = anim_wave_loop()
    t0  = time.time()
    while time.time() - t0 < 1.6:
        yield next(gen)
    yield from _return_home()

def anim_wiggle():
    t0 = time.time()
    while time.time() - t0 < 1.0:
        t  = time.time() - t0
        tg = home_targets()
        w  = math.sin(t * math.pi * 8) * 15
        for leg in range(1, 7):
            tg[leg]["coxa"] += w * (1 if leg % 2 == 0 else -1)
        yield tg, 90.0, 90.0
    yield from _return_home(20)

def anim_stretch():
    for s in range(50):
        t  = s / 50
        tg = home_targets()
        ex = math.sin(t * math.pi) * 30
        for leg in range(1, 7):
            tg[leg]["coxa"]  += ex * (1 if leg % 2 == 0 else -1)
            tg[leg]["femur"] -= ex * 0.3
        yield tg, 90.0, 90.0
    yield from _return_home()

def anim_excited_once():
    gen = anim_excited_loop()
    t0  = time.time()
    while time.time() - t0 < 2.0:
        yield next(gen)
    yield from _return_home()

ANIM_MAP = {
    "idle":          anim_breathe,
    "loading":       anim_wave_loop,
    "responding":    anim_excited_loop,
    "greeting":      anim_greeting,
    "flinch":        anim_flinch,
    "wave_once":     anim_wave_once,
    "wiggle":        anim_wiggle,
    "stretch":       anim_stretch,
    "excited_once":  anim_excited_once,
}

# ── State machine ─────────────────────────────────────────────────────────────
_state      = "idle"
_state_lock = threading.Lock()

def get_state():
    with _state_lock:
        return _state

def set_state(s):
    with _state_lock:
        global _state
        _state = s

# ── Head tracking ─────────────────────────────────────────────────────────────
_FRAME_W = 640
_FRAME_H = 480

def person_head_target():
    """Read YOLO state, return (pan, tilt) centered on nearest person, or None."""
    try:
        with open(BRAIN_STATE) as f:
            data = json.load(f)
        persons = [d for d in data.get("current_detections", []) if d["label"] == "person"]
        if not persons:
            return None
        p    = persons[0]
        pan  = 90.0 - (p["cx"] - _FRAME_W / 2) / _FRAME_W * 60
        tilt = 90.0 + (p["cy"] - _FRAME_H / 2) / _FRAME_H * 30
        return max(60.0, min(120.0, pan)), max(70.0, min(110.0, tilt))
    except Exception:
        return None

# ── Command listener thread ───────────────────────────────────────────────────
def _cmd_listener():
    last_ts = 0.0
    while True:
        try:
            if os.path.exists(CMD_FILE):
                with open(CMD_FILE) as f:
                    data = json.load(f)
                ts  = float(data.get("ts", 0))
                cmd = data.get("cmd", "idle")
                if ts > last_ts and cmd in ANIM_MAP:
                    last_ts = ts
                    set_state(cmd)
                    print(f"[creature] → {cmd}", flush=True)
        except Exception:
            pass
        time.sleep(0.1)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    print("[Tony Creature] Starting — loading tony_flat home pose", flush=True)
    for leg in range(1, 7):
        for j, a in HOME[leg].items():
            set_servo(leg, j, a)
    set_head("pan",  90)
    set_head("tilt", 90)
    time.sleep(0.5)

    threading.Thread(target=_cmd_listener, daemon=True).start()

    cur_anim  = anim_breathe()
    cur_state = "idle"

    while True:
        t0    = time.time()
        state = get_state()

        if state != cur_state:
            cur_anim  = ANIM_MAP[state]()
            cur_state = state

        try:
            tg, pan_h, tilt_h = next(cur_anim)
        except StopIteration:
            set_state("idle")
            cur_anim  = anim_breathe()
            cur_state = "idle"
            tg, pan_h, tilt_h = next(cur_anim)

        person = person_head_target()
        if person:
            pan_h, tilt_h = person
            head_speed = 2.0
        else:
            head_speed = 1.5

        apply_frame(tg, pan_h, tilt_h, head_speed=head_speed)
        time.sleep(max(0.0, TICK - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Tony Creature] Stopped — returning to flat pose.")
        for leg in range(1, 7):
            for j, a in HOME[leg].items():
                set_servo(leg, j, a)
