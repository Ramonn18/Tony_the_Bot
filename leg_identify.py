#!/usr/bin/env python3
"""
leg_identify.py
Interactive tool to map Tony's 18 servo channels to physical legs and joints.

Label Tony's legs 1-6 with tape before running:
  Top-down view:
       FRONT
    1       2
    3       4
    5       6
       BACK

Run directly on the Pi:
  python3 leg_identify.py
"""
import time
import json
import os
from adafruit_servokit import ServoKit

MAP_FILE   = os.path.join(os.path.dirname(__file__), "servo_map.json")
PULSE      = (500, 2500)
BOARDS     = [0x40, 0x41]
CHANNELS   = 9   # channels 0-8 used per board (3 legs × 3 joints)
CENTER     = 90
WIGGLE_DEG = 25  # sweep ±25° to show motion clearly

JOINT_NAMES = ["coxa (hip — rotates leg fwd/back)",
               "femur (thigh — raises/lowers leg)",
               "tibia (shin — extends/curls foot)"]
JOINT_KEYS  = ["coxa", "femur", "tibia"]

def init_boards():
    kits = {}
    for addr in BOARDS:
        try:
            kit = ServoKit(channels=16, address=addr)
            for ch in range(16):
                kit.servo[ch].set_pulse_width_range(*PULSE)
            kits[addr] = kit
            print(f"  Board 0x{addr:02X} OK")
        except Exception as e:
            print(f"  Board 0x{addr:02X} FAILED: {e}")
    return kits

def wiggle(kit, ch):
    """Sweep servo from center → +WIGGLE → center → -WIGGLE → center."""
    kit.servo[ch].angle = CENTER
    time.sleep(0.3)
    for angle in range(CENTER, CENTER + WIGGLE_DEG, 2):
        kit.servo[ch].angle = angle; time.sleep(0.02)
    time.sleep(0.2)
    for angle in range(CENTER + WIGGLE_DEG, CENTER - WIGGLE_DEG, -2):
        kit.servo[ch].angle = angle; time.sleep(0.02)
    time.sleep(0.2)
    for angle in range(CENTER - WIGGLE_DEG, CENTER, 2):
        kit.servo[ch].angle = angle; time.sleep(0.02)
    kit.servo[ch].angle = CENTER

def ask(prompt, valid=None):
    while True:
        val = input(prompt).strip().lower()
        if valid is None or val in valid:
            return val
        print(f"  Enter one of: {', '.join(valid)}")

def main():
    print("\n" + "=" * 50)
    print("  TONY LEG IDENTIFICATION")
    print("=" * 50)
    print("""
Label Tony's legs with tape before starting:
       FRONT
    1       2
    3       4
    5       6
       BACK

Each leg has 3 joints:
  coxa  = hip   (rotates the whole leg forward/back)
  femur = thigh (raises or lowers the leg)
  tibia = shin  (extends or curls the foot)

Controls during scan:
  1-6        → leg number that moved
  s          → skip (nothing moved / not sure)
  r          → repeat wiggle
  q          → quit and save progress
""")
    input("Press Enter when Tony is labeled and ready...")

    print("\nInitializing servo boards...")
    kits = init_boards()
    if not kits:
        print("No boards found — check I2C and power.")
        return

    # Load any existing partial map
    servo_map = {}
    if os.path.exists(MAP_FILE):
        with open(MAP_FILE) as f:
            servo_map = json.load(f)
        print(f"\nLoaded existing map from {MAP_FILE} ({len(servo_map)} entries)")

    total   = len(BOARDS) * CHANNELS
    scanned = 0

    for addr in BOARDS:
        if addr not in kits:
            continue
        kit = kits[addr]

        for ch in range(CHANNELS):
            key = f"0x{addr:02X}_ch{ch}"
            scanned += 1

            if key in servo_map:
                print(f"\n[{scanned}/{total}] {key} already mapped → "
                      f"Leg {servo_map[key]['leg']} {servo_map[key]['joint']}  (skipping)")
                continue

            print(f"\n{'─'*50}")
            print(f"[{scanned}/{total}]  Board 0x{addr:02X}  Channel {ch}")
            print("Wiggling now — watch Tony closely...")

            while True:
                wiggle(kit, ch)

                resp = ask("\nWhich leg moved? (1-6 / s=skip / r=repeat / q=quit): ",
                           valid=["1","2","3","4","5","6","s","r","q"])

                if resp == "q":
                    _save(servo_map)
                    print("\nProgress saved. Re-run to continue.")
                    return
                if resp == "r":
                    print("Repeating wiggle...")
                    continue
                if resp == "s":
                    servo_map[key] = {"leg": None, "joint": None, "board": f"0x{addr:02X}", "channel": ch}
                    print("  Skipped.")
                    break

                leg_num = int(resp)

                print("\nWhich joint moved?")
                for i, name in enumerate(JOINT_NAMES):
                    print(f"  {i+1}. {name}")
                joint_resp = ask("Joint (1/2/3 / r=repeat / s=skip): ",
                                 valid=["1","2","3","r","s"])

                if joint_resp == "r":
                    continue
                if joint_resp == "s":
                    servo_map[key] = {"leg": leg_num, "joint": None, "board": f"0x{addr:02X}", "channel": ch}
                    break

                joint = JOINT_KEYS[int(joint_resp) - 1]
                servo_map[key] = {
                    "leg":     leg_num,
                    "joint":   joint,
                    "board":   f"0x{addr:02X}",
                    "channel": ch
                }
                print(f"  Mapped → Leg {leg_num} | {joint}")
                break

    _save(servo_map)
    _summary(servo_map)

def _save(servo_map):
    with open(MAP_FILE, "w") as f:
        json.dump(servo_map, f, indent=2)
    print(f"\nMap saved to {MAP_FILE}")

def _summary(servo_map):
    print("\n" + "=" * 50)
    print("  MAPPING SUMMARY")
    print("=" * 50)
    by_leg = {}
    skipped = []
    for key, v in servo_map.items():
        if v["leg"] is None:
            skipped.append(key)
        else:
            by_leg.setdefault(v["leg"], {})[v["joint"]] = key

    for leg in sorted(by_leg):
        joints = by_leg[leg]
        complete = all(j in joints for j in JOINT_KEYS)
        status = "✓ complete" if complete else "⚠ incomplete"
        print(f"\n  Leg {leg}  [{status}]")
        for j in JOINT_KEYS:
            print(f"    {j:6s} → {joints.get(j, '?')}")

    if skipped:
        print(f"\n  Skipped / unknown: {', '.join(skipped)}")

    unmapped = [k for k, v in servo_map.items() if v["leg"] is None and v["joint"] is None]
    if unmapped:
        print(f"\n  ⚠ {len(unmapped)} channels produced no movement — check wiring.")

    print("\nRun calibration.py next to verify all legs.")

if __name__ == "__main__":
    main()
