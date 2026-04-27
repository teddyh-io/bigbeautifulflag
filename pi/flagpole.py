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
 POLL_INTERVAL           60 (seconds)
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
from datetime import datetime, timezone

from dotenv import load_dotenv

from arduino import Arduino
from matrix import MatrixScroller, clean_body
from truth import TruthPoller, compute
from vision import describe_media


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


class _PollState:
    """Latest Truth Social fetch result, shared between the fetch + tick loops."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.post_time: datetime | None = None


def _fetch_loop(
    poller: TruthPoller,
    scroller: MatrixScroller,
    state: _PollState,
    interval: int,
    stop: threading.Event,
) -> None:
    """Poll Truth Social every ``interval`` seconds.

    Only touches the network and the RGB matrix body.  The motor + 7-seg
    updates live in :func:`_tick_loop` so the flag can step forward as
    soon as a 30-minute boundary crosses instead of waiting for the next
    poll.
    """
    log = logging.getLogger("fetch")
    last_id: str | None = None
    seen_first = False

    while not stop.is_set():
        try:
            snap = poller.fetch()
        except Exception as exc:   # network / auth issues — keep service alive
            log.exception("fetch failed: %s", exc)
            snap = None

        if snap is not None:
            if snap.id != last_id:
                # Build what to scroll. Every post with attached media gets
                # an OpenAI vision description; if the post also has body
                # text, the description is appended after it on a new line.
                # Vision failures — missing key, network, etc. — silently
                # fall back to the body alone (or "(no post)" if empty).
                display_body = snap.body_html
                if snap.media_url:
                    description = describe_media(
                        snap.media_url,
                        kind=snap.media_kind or "image",
                    )
                    if description:
                        if clean_body(snap.body_html):
                            display_body = f"{snap.body_html}\n{description}"
                        else:
                            display_body = description

                # Only flash the "NEW TRUTH" alert for genuinely new posts —
                # the first fetch after boot just loads whatever the latest
                # post already was, so it shows up directly.
                scroller.set_body(display_body, alert=seen_first)
                last_id = snap.id
                seen_first = True
            with state.lock:
                state.post_time = snap.created_at

        if stop.wait(interval):
            break


def _tick_loop(
    arduino: Arduino,
    state: _PollState,
    stop: threading.Event,
    tick_seconds: float = 1.0,
    resync_seconds: float = 30.0,
) -> None:
    """Recompute percent + countdown every second from the last post time.

    The Uno ticks its own countdown display locally, but the *percent* and
    *motor* only move when the Pi sends a fresh ``G`` / ``P``.  Running
    :func:`compute` at 1 Hz means the flag steps within a second of each
    30-minute boundary (instead of lagging by up to one poll interval) and
    the Uno's countdown gets resynced right away so it jumps back to
    30:00 on the new step instead of sitting at 00:00 until the next poll.
    """
    log = logging.getLogger("ticker")
    last_pct: int | None = None
    last_post_time: datetime | None = None
    last_resync = 0.0

    while not stop.is_set():
        with state.lock:
            post_time = state.post_time

        if post_time is not None:
            now = datetime.now(timezone.utc)
            pct, countdown = compute(now, post_time)
            arduino.set_motor_percent(pct)
            arduino.set_percent_display(pct)

            mono = time.monotonic()
            post_changed = post_time != last_post_time
            if (
                pct != last_pct
                or post_changed
                or mono - last_resync >= resync_seconds
            ):
                arduino.set_countdown(countdown)
                last_resync = mono

            if pct != last_pct:
                prev = "?" if last_pct is None else f"{last_pct}%"
                note = " (new post)" if post_changed and last_pct is not None else ""
                log.info("flag %s%s → %d%% (countdown=%ds)", prev, note, pct, countdown)

            last_pct = pct
            last_post_time = post_time

        if stop.wait(tick_seconds):
            break


def main() -> int:
    load_dotenv()
    _configure_logging()
    _check_credentials()

    port = os.getenv("ARDUINO_PORT", "/dev/ttyACM0")
    handle = os.getenv("TRUTH_HANDLE", "realdonaldtrump")
    interval = int(os.getenv("POLL_INTERVAL", "60"))

    log = logging.getLogger("flagpole")
    log.info("starting — port=%s handle=@%s interval=%ds", port, handle, interval)

    arduino = Arduino(port)
    scroller = MatrixScroller()
    poller = TruthPoller(handle)
    state = _PollState()

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()
        scroller.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    render_thread = threading.Thread(target=scroller.run, name="matrix", daemon=True)
    render_thread.start()

    fetch_thread = threading.Thread(
        target=_fetch_loop,
        args=(poller, scroller, state, interval, stop),
        name="fetcher",
        daemon=True,
    )
    fetch_thread.start()

    tick_thread = threading.Thread(
        target=_tick_loop,
        args=(arduino, state, stop),
        name="ticker",
        daemon=True,
    )
    tick_thread.start()

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        stop.set()
        scroller.stop()
        render_thread.join(timeout=2.0)
        fetch_thread.join(timeout=2.0)
        tick_thread.join(timeout=2.0)
        arduino.close()
        log.info("bye")

    return 0


if __name__ == "__main__":
    sys.exit(main())
