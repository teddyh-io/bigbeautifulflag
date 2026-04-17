#!/usr/bin/env python3
"""Interactive flagpole calibration over SSH.

Run this on the Raspberry Pi 5 after stopping ``flagpole.service`` (the service
holds the serial port exclusively).  Jog to each endpoint, press ``L`` to mark
the LOW (0%) position, then jog up and press ``H`` to mark HIGH (100%).
Calibration is persisted in Arduino EEPROM.

Usage::

    sudo systemctl stop flagpole.service
    python calibrate.py --port /dev/ttyACM0
    sudo systemctl start flagpole.service
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from arduino import Arduino


HELP = """\
──────────────────────────────────────
       FLAG MOTOR CALIBRATION
──────────────────────────────────────
  ↑ / ↓      Fine jog up / down
  W / S      Coarse jog up / down
  L          Mark LOW  position (0%)
  H          Mark HIGH position (100%)
  R          Reset calibration
  ?          Show status
  0–9        Quick goto (0%–90%)
  Q / ESC    Quit
──────────────────────────────────────
Step 1 → Jog to LOW position, press L
Step 2 → Jog UP to HIGH position, press H
"""


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        default=os.getenv("ARDUINO_PORT", "/dev/ttyACM0"),
        help="Serial port (default: %(default)s)",
    )
    parser.add_argument("--baud", type=int, default=9600)
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(message)s")

    import readchar

    ard = Arduino(args.port, args.baud)
    print(HELP)

    try:
        while True:
            key = readchar.readkey()

            if key == readchar.key.UP:
                ard.jog("U", coarse=False)
            elif key == readchar.key.DOWN:
                ard.jog("D", coarse=False)
            elif key.lower() == "w":
                ard.jog("U", coarse=True)
            elif key.lower() == "s":
                ard.jog("D", coarse=True)
            elif key.lower() == "l":
                ard.mark_low()
            elif key.lower() == "h":
                ard.mark_high()
            elif key.lower() == "r":
                ard.reset_calibration()
            elif key == "?":
                ard.status()
            elif key in "0123456789":
                ard.send(f"G{int(key) * 10}")
            elif key.lower() == "q" or key == readchar.key.ESC:
                break
    except KeyboardInterrupt:
        pass
    finally:
        ard.close()
        print("\nCalibration session ended.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
