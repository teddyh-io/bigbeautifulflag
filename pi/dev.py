#!/usr/bin/env python3
"""Dev / demo mode for the Truth Social Flagpole.

Runs the real service logic (matrix scroller, flag-percent math, fetch + tick
loops) with three in-process fakes swapped in for the hardware-touching parts:

    * ``DevMatrixScroller`` subclasses :class:`matrix.MatrixScroller` and
      replaces the Piomatter backend with a plain numpy framebuffer.
    * ``FakeArduino`` mimics the serial wrapper — it just records the last
      ``G`` / ``P`` / ``T`` command so the dev UI can display them.
    * ``FakeTruthPoller`` returns whatever post the browser most recently
      published, instead of hitting Truth Social.

A tiny stdlib HTTP server (no Flask/tk) exposes a single browser page that
shows the scaled-up LED matrix, the two 7-seg displays, the flagpole, and
controls to push a new "truth", rewind the post time, or clear state.

Usage::

    cd pi
    python3 -m venv bbf-venv          # first time only
    bbf-venv/bin/pip install -r requirements-dev.txt
    bbf-venv/bin/python dev.py        # opens http://localhost:8765
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

import matrix as matrix_mod
from flagpole import _PollState, _fetch_loop, _tick_loop
from matrix import MATRIX_H, MATRIX_W, MatrixScroller
from truth import Snapshot, compute

log = logging.getLogger("dev")


# ── fake backends ────────────────────────────────────────────────────────


class FakeArduino:
    """In-process stand-in for :class:`arduino.Arduino`.

    The dev UI only cares about the three commands the main loop actually
    sends (motor %, percent display, countdown), so we expose those as
    attributes and also keep a short rolling log of every command the
    service would have written to the Uno.
    """

    MAX_LOG = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.motor_pct: int = 0
        self.seg_pct: int = -1
        self.countdown: int = -1
        self._log: list[dict] = []
        self._last_sent: dict[str, str] = {}

    def reset(self) -> None:
        """Return to an 'idle' state (no motor, blanked displays).

        The real service never does this — it only ever updates the Uno
        forward — but dev mode needs it so the "Clear post" button can
        visibly reset the flag and the 7-seg readouts.
        """
        with self._lock:
            self._last_sent.clear()
            self.motor_pct = 0
            self.seg_pct = -1
            self.countdown = -1
        self._log_cmd("G0")
        self._log_cmd("P-1")
        self._log_cmd("T-1")

    def _log_cmd(self, cmd: str) -> None:
        entry = {"t": time.time(), "cmd": cmd}
        with self._lock:
            self._log.append(entry)
            if len(self._log) > self.MAX_LOG:
                del self._log[: -self.MAX_LOG]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "motor_pct": self.motor_pct,
                "seg_pct": self.seg_pct,
                "countdown": self.countdown,
                "log": list(self._log[-50:]),
            }

    def set_motor_percent(self, pct: int) -> bool:
        value = str(int(pct))
        if self._last_sent.get("G") == value:
            return False
        self._last_sent["G"] = value
        self._log_cmd(f"G{value}")
        with self._lock:
            self.motor_pct = int(pct)
        return True

    def set_percent_display(self, pct: int) -> bool:
        value = str(int(pct))
        if self._last_sent.get("P") == value:
            return False
        self._last_sent["P"] = value
        self._log_cmd(f"P{value}")
        with self._lock:
            self.seg_pct = int(pct)
        return True

    def set_countdown(self, seconds: int) -> None:
        self._log_cmd(f"T{int(seconds)}")
        with self._lock:
            self.countdown = int(seconds)

    def close(self) -> None:
        pass


class FakeTruthPoller:
    """In-process stand-in for :class:`truth.TruthPoller`.

    Holds whatever (body, created_at) pair the browser last pushed, and
    returns it from :meth:`fetch` the way the real poller returns the
    latest post from the Truth Social API.  ``None`` before the first
    post so the tick loop stays idle until something is published.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._body: Optional[str] = None
        self._created_at: Optional[datetime] = None

    def publish(self, body_html: str, age_minutes: float = 0.0) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._body = body_html
            self._created_at = now - timedelta(minutes=max(0.0, age_minutes))

    def shift_age(self, delta_minutes: float) -> None:
        """Move the current post's ``created_at`` backward in time.

        Useful for demos: press "rewind 30 minutes" a few times and watch
        the flag step up each time without waiting half an hour.
        """
        with self._lock:
            if self._created_at is not None:
                self._created_at -= timedelta(minutes=delta_minutes)

    def clear(self) -> None:
        with self._lock:
            self._body = None
            self._created_at = None

    def snapshot_state(self) -> dict:
        with self._lock:
            return {
                "has_post": self._body is not None,
                "created_at": self._created_at.isoformat() if self._created_at else None,
                "body_preview": (self._body or "")[:200],
            }

    def fetch(self) -> Optional[Snapshot]:
        with self._lock:
            body = self._body
            created_at = self._created_at
        if body is None or created_at is None:
            return None
        now = datetime.now(timezone.utc)
        pct, countdown = compute(now, created_at)
        return Snapshot(
            body_html=body,
            created_at=created_at,
            percent=pct,
            countdown_seconds=countdown,
        )


def _make_alert_frames() -> list[np.ndarray]:
    """Synthesize two 64x32 "NEW TRUTH" frames for the dev alert animation.

    The real deployment uses hand-drawn PNGs in ``TWEET/``; those aren't
    needed for a demo, and this keeps the dev flash working even when the
    art assets haven't been copied over.
    """
    frames: list[np.ndarray] = []
    try:
        font = matrix_mod._load_font()
    except Exception:   # font missing shouldn't break dev mode
        return frames

    for fg, bg, top, bottom in (
        ((255, 255, 255), (200, 0, 0), "NEW", "TRUTH!"),
        ((255, 220, 0), (0, 0, 120), "NEW", "TRUTH!"),
    ):
        img = Image.new("RGB", (MATRIX_W, MATRIX_H), bg)
        draw = ImageDraw.Draw(img)
        draw.text(((MATRIX_W - len(top) * 4) // 2, 8), top, font=font, fill=fg)
        draw.text(((MATRIX_W - len(bottom) * 4) // 2, 18), bottom, font=font, fill=fg)
        frames.append(np.asarray(img, dtype=np.uint8))
    return frames


class DevMatrixScroller(MatrixScroller):
    """MatrixScroller variant that writes frames into a numpy buffer only.

    The parent class's ``run()`` render loop is used verbatim — all we do
    is swap the Piomatter handle for a stub whose ``show()`` copies the
    current framebuffer into a snapshot the HTTP server can read.
    """

    def __init__(self) -> None:
        self._snapshot_lock = threading.Lock()
        self._snapshot_buf = np.zeros((MATRIX_H, MATRIX_W, 3), dtype=np.uint8)
        super().__init__()
        if not self._alert_frames:
            self._alert_frames = _make_alert_frames()

    def _init_matrix(self):
        framebuffer = np.zeros((MATRIX_H, MATRIX_W, 3), dtype=np.uint8)
        parent = self

        class _StubMatrix:
            def show(self_inner) -> None:
                with parent._snapshot_lock:
                    parent._snapshot_buf[:] = framebuffer

        return _StubMatrix(), framebuffer

    def frame_bytes(self) -> bytes:
        """Return the latest framebuffer as raw 64*32*3 RGB bytes."""
        with self._snapshot_lock:
            return self._snapshot_buf.tobytes()


# ── HTTP server ──────────────────────────────────────────────────────────


@dataclass
class DevContext:
    """Everything the HTTP handler needs to read from or write to."""

    arduino: FakeArduino
    poller: FakeTruthPoller
    scroller: DevMatrixScroller
    poll_state: _PollState
    started_at: float


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flagpole — Dev</title>
<style>
  :root {
    --bg: #0b0d12;
    --panel: #141820;
    --panel-2: #1b2130;
    --border: #2a3142;
    --fg: #e6edf3;
    --muted: #8b95a7;
    --red: #e5484d;
    --gold: #f5c518;
    --green: #30a46c;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--fg);
    font-family: ui-sans-serif, system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
  }
  h1 { font-size: 20px; margin: 0; letter-spacing: 1px; }
  h3 { font-size: 13px; margin: 0 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 2px; font-weight: 600; }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    background: var(--panel);
  }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         background: var(--green); margin-right: 8px; box-shadow: 0 0 8px var(--green); }
  main { max-width: 1200px; margin: 24px auto; padding: 0 24px;
         display: grid; grid-template-columns: 1fr 320px; gap: 16px; }
  .panel { background: var(--panel); border: 1px solid var(--border);
           border-radius: 12px; padding: 20px; }
  .panel.tight { padding: 14px 20px; }
  .full { grid-column: 1 / -1; }
  canvas#matrix {
    display: block; width: 100%; max-width: 640px; aspect-ratio: 2;
    image-rendering: pixelated; image-rendering: crisp-edges;
    border-radius: 6px; border: 1px solid #000;
    background: #000;
    margin: 0 auto;
  }
  .status-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .seg {
    background: #05060a; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; text-align: center;
  }
  .seg label { display: block; font-size: 10px; color: var(--muted);
               letter-spacing: 2px; margin-bottom: 6px; }
  .seg .value {
    font-family: "DS-Digital", "Courier New", monospace;
    font-size: 42px; font-weight: bold; color: var(--red);
    text-shadow: 0 0 6px rgba(229,72,77,0.6);
    letter-spacing: 2px;
  }
  .flagpole {
    position: relative;
    height: 220px;
    background: linear-gradient(to bottom, #0a1a2f 0%, #05060a 70%, #0a3210 100%);
    border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden;
  }
  .pole {
    position: absolute;
    left: 50%; top: 14px; bottom: 8px;
    width: 4px;
    background: linear-gradient(#e0e4eb, #7b8494);
    transform: translateX(-50%);
    border-radius: 2px;
  }
  .pole::before {
    content: ""; position: absolute;
    top: -8px; left: 50%; transform: translateX(-50%);
    width: 12px; height: 12px; border-radius: 50%;
    background: radial-gradient(#ffd866, #a17a10);
    box-shadow: 0 0 10px rgba(255,200,80,0.55);
  }
  .flag {
    position: absolute;
    left: calc(50% + 2px);     /* flush against the right side of the pole */
    width: 58px; height: 34px;
    bottom: 8px;               /* JS overrides this with (motor_pct * 0.78)% + 8px */
    background:
      linear-gradient(180deg,
        #b1060f 0%,    #b1060f 14.3%,
        #fff    14.3%, #fff    28.6%,
        #b1060f 28.6%, #b1060f 42.9%,
        #fff    42.9%, #fff    57.1%,
        #b1060f 57.1%, #b1060f 71.4%,
        #fff    71.4%, #fff    85.7%,
        #b1060f 85.7%, #b1060f 100%);
    border: 1px solid #000; border-left: none;
    box-shadow: 2px 2px 0 rgba(0,0,0,0.4);
    transition: bottom 600ms cubic-bezier(.2,.7,.3,1);
  }
  .flag::before {
    /* blue canton in the top-left corner */
    content: ""; position: absolute;
    top: 0; left: 0; width: 24px; height: 57.1%;
    background: #0d47a1;
  }
  .flag-pct {
    position: absolute; right: 10px; top: 8px;
    color: var(--muted); font-size: 11px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  textarea {
    width: 100%; min-height: 90px;
    background: #05060a; color: var(--fg); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px; font: 14px ui-monospace, "SF Mono", Menlo, monospace;
    resize: vertical;
  }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
  button {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 14px; font-size: 13px; cursor: pointer;
    transition: all 150ms;
  }
  button:hover { background: #222a3b; border-color: #3c465c; }
  button.primary { background: var(--red); border-color: var(--red); color: #fff; font-weight: 600; }
  button.primary:hover { background: #c93a3f; border-color: #c93a3f; }
  input[type="number"] {
    background: #05060a; color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 8px; font-size: 13px; width: 80px;
  }
  label { font-size: 13px; color: var(--muted); }
  pre.log {
    background: #05060a; border: 1px solid var(--border); border-radius: 8px;
    padding: 10px; font: 12px ui-monospace, "SF Mono", Menlo, monospace;
    max-height: 180px; overflow-y: auto; margin: 0;
    color: var(--muted);
  }
  pre.log .G { color: var(--gold); }
  pre.log .P { color: #7cb8ff; }
  pre.log .T { color: #a1d05d; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 8px; }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span> TRUTH SOCIAL FLAGPOLE · DEV</h1>
  <span style="font-size: 12px; color: var(--muted);" id="uptime">uptime —</span>
</header>
<main>
  <section class="panel">
    <h3>LED Matrix · 64×32</h3>
    <canvas id="matrix" width="64" height="32"></canvas>
  </section>

  <section class="panel">
    <h3>Flagpole</h3>
    <div class="flagpole">
      <div class="pole">
        <div class="flag" id="flag" style="bottom: 0%"></div>
      </div>
      <div class="flag-pct" id="flagpct">0%</div>
    </div>
    <div class="status-row" style="margin-top: 14px;">
      <div class="seg"><label>FLAG %</label><div class="value" id="seg-pct">--</div></div>
      <div class="seg"><label>NEXT STEP</label><div class="value" id="seg-countdown">--:--</div></div>
    </div>
  </section>

  <section class="panel full">
    <h3>Post a new Truth</h3>
    <textarea id="body" placeholder="Type the body of a new Truth and click Post. The flag will drop to 0% and the matrix will flash + scroll the new text.">Just had the most tremendous meeting folks, BIGGEST EVER! Fake news will never tell you.</textarea>
    <div class="row">
      <label>Posted <input id="age" type="number" value="0" min="0" max="600" step="5"> min ago</label>
      <button class="primary" id="post-btn">Post Truth</button>
      <button id="rewind-btn">Rewind 30 min</button>
      <button id="clear-btn">Clear post</button>
    </div>
    <div class="hint">Tip: <kbd>Post</kbd> with age = 0 drops the flag and plays NEW TRUTH. <kbd>Rewind 30 min</kbd> bumps the flag up one step instantly (no waiting).</div>
  </section>

  <section class="panel full tight">
    <h3>Arduino command log</h3>
    <pre class="log" id="log">(no commands yet)</pre>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const matrixCanvas = $("matrix");
const ctx = matrixCanvas.getContext("2d");
const imgData = ctx.createImageData(64, 32);

function paintMatrix(b64) {
  const bytes = atob(b64);
  for (let i = 0; i < 64 * 32; i++) {
    imgData.data[i * 4    ] = bytes.charCodeAt(i * 3);
    imgData.data[i * 4 + 1] = bytes.charCodeAt(i * 3 + 1);
    imgData.data[i * 4 + 2] = bytes.charCodeAt(i * 3 + 2);
    imgData.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(imgData, 0, 0);
}

function fmtCountdown(sec) {
  if (sec < 0) return "--:--";
  const m = Math.floor(sec / 60), s = sec % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function fmtUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = Math.floor(s % 60);
  return (h ? h + "h " : "") + (m < 10 ? "0" : "") + m + "m " + (ss < 10 ? "0" : "") + ss + "s";
}

async function poll() {
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    paintMatrix(s.matrix);
    $("seg-pct").textContent = s.seg_pct < 0 ? "--" : String(s.seg_pct).padStart(2, "0");
    $("seg-countdown").textContent = fmtCountdown(s.countdown);
    // Flag travels from near the base (~8px) to just below the finial (~78% up).
    $("flag").style.bottom = `calc(8px + ${s.motor_pct * 0.78}%)`;
    $("flagpct").textContent = s.motor_pct + "%";
    $("uptime").textContent = "uptime " + fmtUptime(s.uptime);

    const lines = s.log.slice().reverse().map(e => {
      const d = new Date(e.t * 1000);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      const kind = e.cmd[0];
      return `${hh}:${mm}:${ss}  <span class="${kind}">${e.cmd}</span>`;
    });
    $("log").innerHTML = lines.length ? lines.join("\\n") : "(no commands yet)";
  } catch (err) { console.warn("poll failed", err); }
}

async function api(path, body) {
  return fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
}

$("post-btn").onclick = () => api("/api/post", {
  body: $("body").value,
  age_minutes: parseFloat($("age").value || "0"),
});
$("rewind-btn").onclick = () => api("/api/shift", { minutes: 30 });
$("clear-btn").onclick  = () => api("/api/clear", {});

setInterval(poll, 100);  // 10 Hz — plenty for a 3 fps scroller
poll();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    # Set by _serve() before the server starts.
    ctx: DevContext

    def log_message(self, fmt: str, *args) -> None:  # quiet access log
        log.debug("http: " + fmt, *args)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/state":
            frame = self.ctx.scroller.frame_bytes()
            ard = self.ctx.arduino.snapshot()
            self._send_json({
                "matrix": base64.b64encode(frame).decode("ascii"),
                "motor_pct": ard["motor_pct"],
                "seg_pct": ard["seg_pct"],
                "countdown": ard["countdown"],
                "log": ard["log"],
                "post": self.ctx.poller.snapshot_state(),
                "uptime": time.time() - self.ctx.started_at,
            })
            return

        self.send_error(404, "not found")

    def do_POST(self) -> None:
        # Handlers only mutate the FakeTruthPoller.  The service's existing
        # fetch loop (running at ~3 Hz in dev) will pick up the change on
        # its next iteration and flow it into PollState + the matrix, and
        # the tick loop (10 Hz in dev) will then update the fake Arduino.
        # Total click-to-flag latency is ≲ 400 ms.

        if self.path == "/api/post":
            data = self._read_json()
            body = str(data.get("body", "")).strip()
            age = float(data.get("age_minutes", 0) or 0)
            if not body:
                self._send_json({"ok": False, "error": "empty body"}, 400)
                return
            self.ctx.poller.publish(body, age_minutes=age)
            log.info("dev: posted truth (%d chars, age=%.0fm)", len(body), age)
            self._send_json({"ok": True})
            return

        if self.path == "/api/shift":
            data = self._read_json()
            delta = float(data.get("minutes", 30) or 0)
            self.ctx.poller.shift_age(delta)
            log.info("dev: rewound post by %.0fm", delta)
            self._send_json({"ok": True})
            return

        if self.path == "/api/clear":
            self.ctx.poller.clear()
            # Fetch loop ignores None snaps, so clearing PollState has to
            # happen here — otherwise the tick loop would keep computing
            # against the old post_time.
            with self.ctx.poll_state.lock:
                self.ctx.poll_state.post_time = None
            self.ctx.scroller.set_body("", alert=False)
            # Production never blanks the Uno; dev does, so the 7-seg
            # readouts + motor visibly return to "no data".
            self.ctx.arduino.reset()
            log.info("dev: cleared post")
            self._send_json({"ok": True})
            return

        self.send_error(404, "not found")


def _serve(ctx: DevContext, host: str, port: int) -> ThreadingHTTPServer:
    _Handler.ctx = ctx
    server = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server


# ── entrypoint ───────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: %(default)s)")
    parser.add_argument("--interval", type=float, default=0.3,
                        help="Dev fetch-poll interval in seconds (default: %(default)s; "
                             "low so the matrix flashes + scrolls right after you click Post)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    arduino = FakeArduino()
    poller = FakeTruthPoller()
    scroller = DevMatrixScroller()
    state = _PollState()
    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()
        scroller.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    render_thread = threading.Thread(target=scroller.run, name="matrix", daemon=True)
    fetch_thread = threading.Thread(
        target=_fetch_loop,
        args=(poller, scroller, state, args.interval, stop),
        name="fetcher",
        daemon=True,
    )
    tick_thread = threading.Thread(
        target=_tick_loop,
        # Run the ticker at 10 Hz in dev (production is 1 Hz) so clicks in the
        # browser show up in the flag / 7-seg displays effectively instantly.
        args=(arduino, state, stop),
        kwargs={"tick_seconds": 0.1, "resync_seconds": 30.0},
        name="ticker",
        daemon=True,
    )
    render_thread.start()
    fetch_thread.start()
    tick_thread.start()

    ctx = DevContext(
        arduino=arduino,
        poller=poller,
        scroller=scroller,
        poll_state=state,
        started_at=time.time(),
    )
    server = _serve(ctx, args.host, args.port)

    url = f"http://{args.host}:{args.port}/"
    log.info("dev UI ready at %s", url)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        stop.set()
        scroller.stop()
        server.shutdown()
        render_thread.join(timeout=2.0)
        fetch_thread.join(timeout=2.0)
        tick_thread.join(timeout=2.0)
        arduino.close()
        log.info("bye")

    return 0


if __name__ == "__main__":
    sys.exit(main())
