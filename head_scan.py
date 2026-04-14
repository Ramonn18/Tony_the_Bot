#!/usr/bin/env python3
"""
head_scan.py
Scans PCA9685 channels one at a time to identify the head pan/tilt servos.
Watch Tony's head — when it moves, note the board and channel number.

Controls:
  Enter  → move to next channel
  q      → quit
"""
import time
import sys
from adafruit_servokit import ServoKit

PULSE      = (500, 2500)
CENTER     = 90
TEST_ANGLE = 60    # how far to swing for identification
HOLD_SECS  = 1.5   # how long to hold the test angle

# Channels to scan — starts after leg channels (0-8) on each board
SCAN = [
    (0x40, list(range(9, 16))),   # board 0x40 channels 9-15
    (0x41, list(range(9, 16))),   # board 0x41 channels 9-15
]

_kits = {}
def get_kit(addr):
    if addr not in _kits:
        kit = ServoKit(channels=16, address=addr)
        for ch in range(16):
            kit.servo[ch].set_pulse_width_range(*PULSE)
        _kits[addr] = kit
    return _kits[addr]


def test_channel(addr, ch):
    kit = get_kit(addr)
    print(f"\n  → board 0x{addr:02X}  channel {ch:02d}")
    print(f"     Center ({CENTER}°) ...", end=" ", flush=True)
    kit.servo[ch].angle = CENTER
    time.sleep(0.6)
    print(f"Swing ({TEST_ANGLE}°) ...", end=" ", flush=True)
    kit.servo[ch].angle = TEST_ANGLE
    time.sleep(HOLD_SECS)
    print(f"Back to center.", end="  ", flush=True)
    kit.servo[ch].angle = CENTER
    time.sleep(0.4)


def main():
    print("=" * 50)
    print("  Tony Head Servo Scanner")
    print("  Watch the head — press Enter to advance,")
    print("  type the joint name when it moves (pan/tilt),")
    print("  or press q to quit.")
    print("=" * 50)

    found = {}

    for addr, channels in SCAN:
        print(f"\n--- Board 0x{addr:02X} ---")
        for ch in channels:
            test_channel(addr, ch)
            resp = input("  Did the HEAD move? (pan / tilt / no / q): ").strip().lower()
            if resp == "q":
                break
            if resp in ("pan", "tilt"):
                found[resp] = (addr, ch)
                print(f"  Saved: {resp} = board 0x{addr:02X} channel {ch}")
        else:
            continue
        break

    print("\n" + "=" * 50)
    if found:
        print("Head servo map:")
        for joint, (addr, ch) in found.items():
            print(f"  {joint.upper():<6} → board 0x{addr:02X}  channel {ch}")
        print("\nSave these — we'll use them to build head_control.py")
    else:
        print("No head servos identified yet.")
        print("Try running again and watch carefully for small movements.")

if __name__ == "__main__":
    main()
