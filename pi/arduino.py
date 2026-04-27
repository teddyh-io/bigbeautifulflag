"""Thin wrapper around the Arduino Uno serial link.

The Uno firmware (``firmware/flagpole/flagpole.ino``) listens on 9600 baud
for newline-terminated commands:

======  =====================================================
 cmd    meaning
======  =====================================================
 G<n>   move motor to n% (0-100)
 P<n>   set percent display on Seg1 (-1 blanks it)
 T<n>   start countdown of n seconds on Seg2 (-1 blanks it)
 U/D    fine jog up/down (calibration)
 UU/DD  coarse jog up/down (calibration)
 L/H    mark low/high endpoints (calibration)
 R      reset calibration
 S      print status
======  =====================================================
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

import serial

log = logging.getLogger(__name__)


class Arduino:
    """Serial client for the flagpole Arduino.

    All writes are serialised through a lock so the poller thread, the
    matrix thread, and any ad-hoc calls can share the same port safely.
    A background reader thread forwards every line from the Uno to the
    Python ``logging`` framework.
    """

    def __init__(self, port: str, baud: int = 9600, *, settle_seconds: float = 2.0):
        self._ser = serial.Serial(port, baud, timeout=0.5)
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._last_sent: dict[str, str] = {}

        time.sleep(settle_seconds)  # Uno auto-resets on serial open
        self._reader.start()

    # ── low level ────────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = self._ser.readline()
            except Exception as exc:
                # close() racing with readline() yanks the port's internal
                # buffers and surfaces as a confusing TypeError; swallow it
                # silently if we're already on the way out.
                if self._stop.is_set():
                    return
                log.warning("arduino: read failed: %s", exc)
                time.sleep(0.5)
                continue
            if not line:
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                log.info("arduino: %s", text)

    def send(self, cmd: str) -> None:
        payload = f"{cmd}\n".encode()
        with self._write_lock:
            self._ser.write(payload)
            self._ser.flush()
        log.debug("arduino <- %s", cmd)

    def send_many(self, cmds: Iterable[str]) -> None:
        for cmd in cmds:
            self.send(cmd)

    def close(self) -> None:
        self._stop.set()
        # Let the reader unwind first so it doesn't race with the port
        # being closed (readline() has a 0.5s timeout, so this is bounded).
        if self._reader.is_alive():
            self._reader.join(timeout=1.0)
        try:
            self._ser.close()
        except Exception:
            pass

    # ── high level ───────────────────────────────────────────────────────
    def set_motor_percent(self, pct: int) -> bool:
        """Move the flag to ``pct``; returns True if a command was sent.

        No-ops when the target is unchanged from the previous call so the
        motor doesn't thrash on every poll.
        """
        key = "G"
        value = str(int(pct))
        if self._last_sent.get(key) == value:
            return False
        self._last_sent[key] = value
        self.send(f"G{value}")
        return True

    def set_percent_display(self, pct: int) -> bool:
        key = "P"
        value = str(int(pct))
        if self._last_sent.get(key) == value:
            return False
        self._last_sent[key] = value
        self.send(f"P{value}")
        return True

    def set_countdown(self, seconds: int) -> None:
        """Restart the Uno's countdown. Sent every tick so clock drift resets."""
        self.send(f"T{int(seconds)}")

    # ── calibration helpers used by calibrate.py ─────────────────────────
    def jog(self, direction: str, coarse: bool = False) -> None:
        if direction not in ("U", "D"):
            raise ValueError(direction)
        self.send(direction * (2 if coarse else 1))

    def mark_low(self) -> None:
        self.send("L")

    def mark_high(self) -> None:
        self.send("H")

    def reset_calibration(self) -> None:
        self.send("R")

    def status(self) -> None:
        self.send("S")
