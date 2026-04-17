#!/usr/bin/env python3
"""Truth Social flagpole service (Raspberry Pi 5).

Polls Truth Social, scrolls the latest post body on a 64x32 RGB matrix, and
drives the motor + dual 7-seg displays on the Arduino Uno.

Configuration is via environment variables (typically loaded from
``/etc/flagpole.env`` by systemd, or a local ``.env``):

======================  ==========================================
 env var                 default / notes
======================  ==========================================
 ARDUINO_PORT            /dev/ttyACM0
 TRUTH_HANDLE            realdonaldtrump
 POLL_INTERVAL           300 (seconds)
 TRUTHSOCIAL_USERNAME    required unless TRUTHSOCIAL_TOKEN is set
 TRUTHSOCIAL_PASSWORD    required unless TRUTHSOCIAL_TOKEN is set
 TRUTHSOCIAL_TOKEN       optional bearer token alternative
======================  ==========================================
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from dotenv import load_dotenv

from arduino import Arduino
from matrix import MatrixScroller
from truth import TruthPoller


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _check_credentials() -> None:
    if os.getenv("TRUTHSOCIAL_TOKEN"):
        return
    if os.getenv("TRUTHSOCIAL_USERNAME") and os.getenv("TRUTHSOCIAL_PASSWORD"):
        return
    sys.stderr.write(
        "Truth Social credentials missing. Set TRUTHSOCIAL_USERNAME+TRUTHSOCIAL_PASSWORD "
        "or TRUTHSOCIAL_TOKEN (e.g. in /etc/flagpole.env).\n"
    )
    sys.exit(1)


def _poll_loop(
    poller: TruthPoller,
    arduino: Arduino,
    scroller: MatrixScroller,
    interval: int,
    stop: threading.Event,
) -> None:
    log = logging.getLogger("poller")
    last_body: str | None = None

    while not stop.is_set():
        try:
            snap = poller.fetch()
        except Exception as exc:   # network / auth issues — keep service alive
            log.exception("fetch failed: %s", exc)
            snap = None

        if snap is not None:
            if snap.body_html != last_body:
                scroller.set_body(snap.body_html)
                last_body = snap.body_html
            arduino.set_motor_percent(snap.percent)
            arduino.set_percent_display(snap.percent)
            arduino.set_countdown(snap.countdown_seconds)

        if stop.wait(interval):
            break


def main() -> int:
    load_dotenv()
    _configure_logging()
    _check_credentials()

    port = os.getenv("ARDUINO_PORT", "/dev/ttyACM0")
    handle = os.getenv("TRUTH_HANDLE", "realdonaldtrump")
    interval = int(os.getenv("POLL_INTERVAL", "300"))

    log = logging.getLogger("flagpole")
    log.info("starting — port=%s handle=@%s interval=%ds", port, handle, interval)

    arduino = Arduino(port)
    scroller = MatrixScroller()
    poller = TruthPoller(handle)

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()
        scroller.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    render_thread = threading.Thread(target=scroller.run, name="matrix", daemon=True)
    render_thread.start()

    poll_thread = threading.Thread(
        target=_poll_loop,
        args=(poller, arduino, scroller, interval, stop),
        name="poller",
        daemon=True,
    )
    poll_thread.start()

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        stop.set()
        scroller.stop()
        render_thread.join(timeout=2.0)
        poll_thread.join(timeout=2.0)
        arduino.close()
        log.info("bye")

    return 0


if __name__ == "__main__":
    sys.exit(main())
