#!/usr/bin/env python3
"""
servo_center_test.py
Moves all 18 servos on Tony's PCA9685 boards to center (90°).
Run this first to verify all servos are alive and responding.

Leg layout (Freenove Big Hexapod):
  Board 0x40 — channels 0-8  (legs 1-3)
  Board 0x41 — channels 0-8  (legs 4-6)
  Each leg: channel 0=coxa, 1=femur, 2=tibia  (×3 legs per board)
"""
import time
from adafruit_servokit import ServoKit

BOARDS = [0x40, 0x41]
CHANNELS_PER_BOARD = 9   # 3 legs × 3 servos
CENTER = 90              # degrees

print("Tony servo center test")
print("=" * 40)

for addr in BOARDS:
    print(f"\nInitializing board at 0x{addr:02X}...")
    try:
        kit = ServoKit(channels=16, address=addr)
        for ch in range(CHANNELS_PER_BOARD):
            kit.servo[ch].set_pulse_width_range(500, 2500)
            kit.servo[ch].angle = CENTER
            print(f"  0x{addr:02X} ch{ch:02d} → {CENTER}°")
            time.sleep(0.05)
        print(f"  Board 0x{addr:02X} OK — {CHANNELS_PER_BOARD} servos centered")
    except Exception as e:
        print(f"  Board 0x{addr:02X} ERROR: {e}")

print("\nDone. All servos should be at center position.")
print("If any servo twitched or didn't move, note its board/channel.")
