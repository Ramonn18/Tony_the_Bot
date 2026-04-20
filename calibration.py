#!/usr/bin/env python3
"""
calibration.py
Startup calibration for Tony.

Reads servo_map.json, sweeps every mapped joint through its range,
and reports a pass/fail health check per leg.

Can be imported and called from other scripts:
    from calibration import run_calibration
    run_calibration()

Or run directly:
    python3 calibration.py
"""
import time
import json
import os
from adafruit_servokit import ServoKit

MAP_FILE = os.path.join(os.path.dirname(__file__), "servo_map.json")
PULSE    = (500, 2500)
CENTER   = 90
SPEED    = 0.012  # seconds per degree step

# Per-joint safe sweep range (degrees from center)
JOINT_RANGE = {
    "coxa":  20,  # hip — gentle side sweep
    "femur": 25,  # thigh — lift test
    "tibia": 25,  # shin — curl test
}

_kits = {}

def _get_kit(addr_str):
    addr = int(addr_str, 16)
    if addr not in _kits:
        kit = ServoKit(channels=16, address=addr)
        for ch in range(16):
            kit.servo[ch].set_pulse_width_range(*PULSE)
        _kits[addr] = kit
    return _kits[addr]

def _sweep(kit, ch, sweep_deg, speed=SPEED):
    """Center → +sweep → center → -sweep → center. Returns True if no exception."""
    try:
        kit.servo[ch].angle = CENTER
        time.sleep(0.2)
        for a in range(CENTER, CENTER + sweep_deg, 2):
            kit.servo[ch].angle = a; time.sleep(speed)
        for a in range(CENTER + sweep_deg, CENTER - sweep_deg, -2):
            kit.servo[ch].angle = a; time.sleep(speed)
        for a in range(CENTER - sweep_deg, CENTER + 1, 2):
            kit.servo[ch].angle = a; time.sleep(speed)
        kit.servo[ch].angle = CENTER
        return True
    except Exception as e:
        print(f"    ERROR on {kit} ch{ch}: {e}")
        return False

def run_calibration(verbose=True):
    """
    Run full startup calibration.
    Returns a results dict: {leg_num: {joint: 'pass'|'fail'|'skip'}}
    """
    results = {}

    if not os.path.exists(MAP_FILE):
        print("  No servo_map.json found — run leg_identify.py first.")
        return results

    with open(MAP_FILE) as f:
        servo_map = json.load(f)

    if verbose:
        print("\n" + "=" * 50)
        print("  TONY STARTUP CALIBRATION")
        print("=" * 50)

    # Group by leg for ordered output
    by_leg = {}
    for key, v in servo_map.items():
        if v.get("leg") and v.get("joint"):
            leg = v["leg"]
            by_leg.setdefault(leg, {})[v["joint"]] = (v["board"], v["channel"])

    if not by_leg:
        print("  Map is empty or incomplete — run leg_identify.py first.")
        return results

    all_pass = True

    for leg in sorted(by_leg):
        results[leg] = {}
        if verbose:
            print(f"\n  Leg {leg}")

        for joint in ["coxa", "femur", "tibia"]:
            if joint not in by_leg[leg]:
                results[leg][joint] = "skip"
                if verbose:
                    print(f"    {joint:6s} → SKIP (not mapped)")
                continue

            board_str, ch = by_leg[leg][joint]
            sweep = JOINT_RANGE.get(joint, 20)

            try:
                kit  = _get_kit(board_str)
                ok   = _sweep(kit, ch, sweep)
                status = "pass" if ok else "fail"
            except Exception as e:
                status = "fail"
                if verbose:
                    print(f"    {joint:6s} → FAIL  (board {board_str} ch{ch}: {e})")

            results[leg][joint] = status
            if status == "fail":
                all_pass = False

            if verbose:
                icon = "✓" if status == "pass" else "✗"
                print(f"    {joint:6s} → {icon} {status.upper()}  "
                      f"(board {board_str} ch{ch})")

        time.sleep(0.2)

    if verbose:
        print("\n" + "─" * 50)
        if all_pass:
            print("  CALIBRATION PASSED — all joints responding")
        else:
            fails = [(l, j) for l, joints in results.items()
                     for j, s in joints.items() if s == "fail"]
            print(f"  CALIBRATION FAILED — {len(fails)} joint(s) did not respond:")
            for leg, joint in fails:
                print(f"    Leg {leg} {joint}")
        print("─" * 50)

    return results

if __name__ == "__main__":
    run_calibration(verbose=True)
