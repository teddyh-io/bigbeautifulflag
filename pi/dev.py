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
from matrix import MatrixScroller, clean_body
from truth import compute
from vision import describe_media


log = logging.getLogger("dev")

# Same path systemd uses (EnvironmentFile= in pi/systemd/flagpole.service).
# Loaded automatically so URL-paste / vision auth "just works" on the Pi
# without having to copy secrets next to the script.
SYSTEM_ENV_FILE = "/etc/flagpole.env"


BANNER = """\
──────────────────────────────────────
         FLAGPOLE DEV MODE
──────────────────────────────────────
  <text>       Post <text> now (resets flag, plays NEW TRUTH)
  post <body>  Same as above, explicit form
  <truth url>  Pull a real post by URL and inject it as a new truth
               e.g. https://truthsocial.com/@realDonaldTrump/1234...
               (any post with media gets an OpenAI vision description
               appended, same as the live service)
  age <dur>    Rewind last post by <dur> — e.g. 30s, 45m, 2h, 5h30m
  state / ?    Print current percent, countdown, last post age
  help / h     Show this banner
  quit / q     Exit (Ctrl-C or EOF also work)
──────────────────────────────────────
"""


_DUR_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?truthsocial\.com/@([^/\s]+)/(\d+)(?:[/?#]|$)",
    re.IGNORECASE,
)
_DEFAULT_HANDLE = "realdonaldtrump"


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


def _parse_post_url(line: str) -> tuple[str, str] | None:
    """Return ``(handle, post_id)`` for a Truth Social post URL, else ``None``."""
    m = _POST_URL_RE.match(line.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _fetch_status_by_id(post_id: str) -> dict | None:
    """Fetch a single Truth Social status by ID via truthbrush.

    truthbrush has no public single-status helper, so we hit the underlying
    Mastodon-compatible endpoint through ``Api._get``. ``TRUTHSOCIAL_TOKEN``
    in the env (loaded by truthbrush itself at import) is enough to auth;
    we fall back to a no-op ``lookup()`` to force a username/password login
    when only those are set.
    """
    try:
        from truthbrush.api import Api
    except ImportError as exc:
        log.warning("truth: truthbrush not installed: %s", exc)
        return None

    api = Api()
    if api.auth_id is None:
        try:
            api.lookup(_DEFAULT_HANDLE)
        except Exception as exc:
            log.warning("truth: login failed: %s", exc)
            return None

    try:
        status = api._get(f"/v1/statuses/{post_id}")
    except Exception as exc:
        log.warning("truth: GET /v1/statuses/%s failed: %s", post_id, exc)
        return None

    if not isinstance(status, dict) or "content" not in status:
        log.warning("truth: unexpected response for %s: %r", post_id, status)
        return None
    return status


def _resolve_display_body(status: dict) -> str:
    """Pick the matrix body for a fetched status.

    Mirrors the behaviour of ``flagpole._fetch_loop``: if the post has
    media attached, we run an OpenAI vision description and append it
    after the body text (or use it alone when there's no caption). If
    vision is unavailable, we fall through to the original body.
    """
    body = status.get("content", "") or ""

    media_url: str | None = None
    media_kind: str | None = None
    for att in status.get("media_attachments") or []:
        att_type = att.get("type")
        if att_type == "image" and att.get("url"):
            media_url = att["url"]
            media_kind = "image"
            break
        if att_type in ("video", "gifv") and att.get("preview_url"):
            media_url = att["preview_url"]
            media_kind = "video"
            break

    if not media_url:
        return body

    description = describe_media(media_url, kind=media_kind or "image")
    if not description:
        return body
    if clean_body(body):
        return f"{body}\n{description}"
    return description


def _inject_from_url(
    scroller: MatrixScroller,
    state: _PollState,
    line: str,
) -> bool:
    """If ``line`` is a Truth Social post URL, fetch it and inject it.

    Returns True iff the line looked like a URL we tried to handle (so the
    caller can skip the plain-text-post fallback). Network/auth/vision
    failures are reported on stdout but still return True — the user
    pasted a URL on purpose, so we don't want to silently treat it as
    post body text.
    """
    parsed = _parse_post_url(line)
    if parsed is None:
        return False

    handle, post_id = parsed
    print(f"fetching @{handle}/{post_id} ...", flush=True)
    status = _fetch_status_by_id(post_id)
    if status is None:
        print(f"  could not fetch post {post_id} (see log)", flush=True)
        return True

    display = _resolve_display_body(status)
    _inject_post(scroller, state, display, alert=True)

    rendered_chars = len(clean_body(display))
    media_note = ""
    kinds = [att.get("type") for att in (status.get("media_attachments") or [])]
    if kinds:
        body_chars = len(clean_body(status.get("content", "") or ""))
        described = rendered_chars > body_chars
        media_note = f" [media: {', '.join(kinds)}; vision={'on' if described else 'off'}]"
    print(
        f"posted @{handle}/{post_id} ({rendered_chars} chars on matrix)"
        f"{media_note} — flag reset, NEW TRUTH alert playing",
        flush=True,
    )
    return True


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
            # A bare Truth Social URL → fetch the real post and inject it.
            if _inject_from_url(scroller, state, line):
                continue
            # Any other line is treated as the post body verbatim.
            body = line

        _inject_post(scroller, state, body, alert=True)
        print(
            f"posted ({len(body)} chars) — flag reset, NEW TRUTH alert playing",
            flush=True,
        )


def _load_env() -> bool:
    """Populate os.environ from the same sources the live service sees.

    ``load_dotenv()`` only fills variables that aren't already set, so the
    ordering here is: a developer-local ``.env`` (walked up from the cwd)
    wins over the production secrets file at ``/etc/flagpole.env``, which
    in turn wins over a totally empty environment. Returns True if the
    system env file was found and loaded (logged once basicConfig is up).
    """
    load_dotenv()
    if os.path.isfile(SYSTEM_ENV_FILE):
        load_dotenv(SYSTEM_ENV_FILE)
        return True
    return False


def main() -> int:
    # Load env BEFORE argparse / basicConfig so ARDUINO_PORT and LOG_LEVEL
    # picked up from /etc/flagpole.env take effect.
    loaded_system_env = _load_env()

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

    if loaded_system_env:
        log.info("env: loaded %s", SYSTEM_ENV_FILE)
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
