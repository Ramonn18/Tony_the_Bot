#!/usr/bin/env python3
"""
leg_test.py
Tests a single leg on Tony by moving coxa, femur, and tibia
through a simple lift sequence.

Freenove channel mapping (board 0x40):
  Leg 1: ch0=coxa  ch1=femur  ch2=tibia
  Leg 2: ch3=coxa  ch4=femur  ch5=tibia
  Leg 3: ch6=coxa  ch7=femur  ch8=tibia

Freenove channel mapping (board 0x41):
  Leg 4: ch0=coxa  ch1=femur  ch2=tibia
  Leg 5: ch3=coxa  ch4=femur  ch5=tibia
  Leg 6: ch6=coxa  ch7=femur  ch8=tibia

Usage:
  python3 leg_test.py          → tests leg 1 (default)
  python3 leg_test.py 3        → tests leg 3
"""
import time
import sys
from adafruit_servokit import ServoKit

PULSE = (500, 2500)   # min/max pulse width for Freenove servos
SPEED = 0.015         # seconds between degree steps (lower = faster)

# Board and channel layout per leg
LEG_MAP = {
    1: (0x40, 0, 1, 2),
    2: (0x40, 3, 4, 5),
    3: (0x40, 6, 7, 8),
    4: (0x41, 0, 1, 2),
    5: (0x41, 3, 4, 5),
    6: (0x41, 6, 7, 8),
}

# Cache kit instances so we don't re-init the same board twice
_kits = {}

def get_kit(addr):
    if addr not in _kits:
        kit = ServoKit(channels=16, address=addr)
        for ch in range(16):
            kit.servo[ch].set_pulse_width_range(*PULSE)
        _kits[addr] = kit
    return _kits[addr]


def move_servo(kit, ch, target, current=None):
    """Smoothly move a servo from current angle to target angle."""
    if current is None:
        kit.servo[ch].angle = target
        time.sleep(0.1)
        return target
    step = 1 if target > current else -1
    for angle in range(int(current), int(target) + step, step):
        kit.servo[ch].angle = angle
        time.sleep(SPEED)
    return target


def test_leg(leg_num):
    if leg_num not in LEG_MAP:
        print(f"Invalid leg number. Choose 1-6.")
        return

    addr, coxa_ch, femur_ch, tibia_ch = LEG_MAP[leg_num]
    kit = get_kit(addr)

    print(f"\nTesting Leg {leg_num} (board 0x{addr:02X})")
    print(f"  Coxa  → ch{coxa_ch}")
    print(f"  Femur → ch{femur_ch}")
    print(f"  Tibia → ch{tibia_ch}")
    print("-" * 35)

    # Step 1 — center all three joints
    print("Step 1: Centering leg...")
    coxa  = move_servo(kit, coxa_ch,  90)
    femur = move_servo(kit, femur_ch, 90)
    tibia = move_servo(kit, tibia_ch, 90)
    time.sleep(0.5)

    # Step 2 — lift: raise femur up, curl tibia in
    print("Step 2: Lifting leg...")
    femur = move_servo(kit, femur_ch, 60,  femur)   # raise thigh
    tibia = move_servo(kit, tibia_ch, 120, tibia)   # curl shin
    time.sleep(0.4)

    # Step 3 — swing coxa forward
    print("Step 3: Swinging forward...")
    coxa = move_servo(kit, coxa_ch, 110, coxa)
    time.sleep(0.4)

    # Step 4 — lower leg back down
    print("Step 4: Lowering leg...")
    femur = move_servo(kit, femur_ch, 90, femur)
    tibia = move_servo(kit, tibia_ch, 90, tibia)
    time.sleep(0.4)

    # Step 5 — return coxa to center
    print("Step 5: Returning to center...")
    coxa = move_servo(kit, coxa_ch, 90, coxa)
    time.sleep(0.5)

    print(f"\nLeg {leg_num} test complete.")
    print("Did the leg move? If yes — servos are live and ready.")
    print("If no — check battery charge on the load switch.")


if __name__ == "__main__":
    leg = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    test_leg(leg)
