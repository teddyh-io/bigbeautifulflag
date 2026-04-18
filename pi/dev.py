#!/usr/bin/env python3
"""Truth Social flagpole DEV / DEMO MODE.

Runs the same motor + matrix + countdown pipeline as :mod:`flagpole`, but
instead of polling Truth Social, takes fake posts from stdin so you can
demo or debug the rig without waiting for @realdonaldtrump to post.

Usage::

    sudo systemctl stop flagpole.service   # release /dev/ttyACM0 + matrix
    cd ~/bigbeautifulflag/pi
    bbf-venv/bin/python dev.py
    # ... type fake posts, age them, etc ...
    sudo systemctl start flagpole.service  # when done
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from arduino import Arduino
from flagpole import _PollState, _tick_loop
from matrix import MatrixScroller
from truth import compute


log = logging.getLogger("dev")


BANNER = """\
──────────────────────────────────────
         FLAGPOLE DEV MODE
──────────────────────────────────────
  <text>       Post <text> now (resets flag, plays NEW TRUTH)
  post <body>  Same as above, explicit form
  age <dur>    Rewind last post by <dur> — e.g. 30s, 45m, 2h, 5h30m
  state / ?    Print current percent, countdown, last post age
  help / h     Show this banner
  quit / q     Exit (Ctrl-C or EOF also work)
──────────────────────────────────────
"""


_DUR_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _parse_duration(s: str) -> timedelta | None:
    """Parse '30s' / '45m' / '2h' / '5h30m' style strings. Returns None if invalid."""
    m = _DUR_RE.match(s.strip())
    if not m or not any(m.groups()):
        return None
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=se)


def _inject_post(
    scroller: MatrixScroller,
    state: _PollState,
    body: str,
    *,
    alert: bool,
) -> None:
    """Push a fake post into the shared state + matrix."""
    scroller.set_body(body, alert=alert)
    with state.lock:
        state.post_time = datetime.now(timezone.utc)


def _print_state(state: _PollState) -> None:
    with state.lock:
        pt = state.post_time
    if pt is None:
        print("no post yet", flush=True)
        return
    now = datetime.now(timezone.utc)
    age = (now - pt).total_seconds()
    pct, countdown = compute(now, pt)
    print(
        f"post age={age:.0f}s  pct={pct}%  "
        f"countdown={'—' if countdown < 0 else f'{countdown}s'}",
        flush=True,
    )


def _stdin_loop(
    scroller: MatrixScroller,
    state: _PollState,
    stop: threading.Event,
) -> None:
    """Read commands from stdin until the user quits or hits EOF."""
    print(BANNER, flush=True)

    # Seed an initial body so the matrix isn't blank at boot. alert=False
    # mirrors the real service's first-fetch-after-boot behaviour — the
    # NEW TRUTH flash only fires when there was a previous body.
    _inject_post(
        scroller,
        state,
        "FLAGPOLE DEV MODE. Type a truth.",
        alert=False,
    )

    while not stop.is_set():
        try:
            line = input("> ")
        except EOFError:
            print("", flush=True)
            stop.set()
            return
        except KeyboardInterrupt:
            stop.set()
            return

        line = line.strip()
        if not line:
            continue

        cmd, _, rest = line.partition(" ")
        cmd_l = cmd.lower()

        if cmd_l in ("quit", "q", "exit"):
            stop.set()
            return

        if cmd_l in ("help", "h"):
            print(BANNER, flush=True)
            continue

        if cmd_l in ("state", "?"):
            _print_state(state)
            continue

        if cmd_l == "age":
            dur = _parse_duration(rest)
            if dur is None:
                print("usage: age <dur>  e.g. 30s, 45m, 2h, 5h30m", flush=True)
                continue
            with state.lock:
                if state.post_time is None:
                    print("no post yet — post something first", flush=True)
                    continue
                state.post_time = datetime.now(timezone.utc) - dur
            print(f"rewound last post to {dur} ago", flush=True)
            continue

        if cmd_l == "post":
            body = rest
            if not body:
                print("usage: post <body>", flush=True)
                continue
        else:
            # Any other line is treated as the post body verbatim.
            body = line

        _inject_post(scroller, state, body, alert=True)
        print(
            f"posted ({len(body)} chars) — flag reset, NEW TRUTH alert playing",
            flush=True,
        )


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Flagpole dev / demo mode")
    parser.add_argument(
        "--port",
        default=os.getenv("ARDUINO_PORT", "/dev/ttyACM0"),
        help="Serial port for the Arduino Uno (default: %(default)s)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info("starting dev mode — port=%s", args.port)

    arduino = Arduino(args.port)
    scroller = MatrixScroller()
    state = _PollState()
    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()
        scroller.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    render_thread = threading.Thread(
        target=scroller.run, name="matrix", daemon=True
    )
    render_thread.start()

    tick_thread = threading.Thread(
        target=_tick_loop,
        args=(arduino, state, stop),
        name="ticker",
        daemon=True,
    )
    tick_thread.start()

    try:
        _stdin_loop(scroller, state, stop)
    finally:
        stop.set()
        scroller.stop()
        render_thread.join(timeout=2.0)
        tick_thread.join(timeout=2.0)
        arduino.close()
        log.info("bye")

    return 0


if __name__ == "__main__":
    sys.exit(main())
