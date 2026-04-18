# Truth Social Flagpole

A physical installation that raises an American flag when
[@realDonaldTrump](https://truthsocial.com/@realDonaldTrump) has gone too
long without posting on Truth Social, and lowers it again every time he posts.

## Behaviour

- Poll Truth Social every minute (configurable).
- Flag rises 10% for every 30 minutes of silence, capping at 100%
  (i.e. fully raised after 5 h 30 m of no new posts).
- Any new post drops the flag back to 0%.
- A 64x32 RGB matrix scrolls the body of the latest post.
- When a new post is detected the matrix flashes a "NEW TRUTH" alert
  (two frames alternating every 200 ms for 5 s) before the new body
  scrolls in.
- One HW-069 7-seg displays the current flag % and another counts down the
  seconds until the next 10% step.

## Repo layout

```
arduino/
  flagpole/
    flagpole.ino       # Uno sketch (motor + 2x TM1637)

pi/
  flagpole.py          # main systemd service
  calibrate.py         # standalone interactive calibration over SSH
  dev.py               # browser-based dev/demo (no hardware, no Truth Social)
  matrix.py            # Piomatter + tom-thumb BDF scroller
  arduino.py           # serial protocol wrapper for the Uno
  truth.py             # polling + percent/countdown math
  fonts/
    tom-thumb.bdf      # MIT-licensed 4x6 pixel font
  systemd/
    flagpole.service   # systemd unit (points at /home/teddyh/bigbeautifulflag/pi)
  requirements.txt
  requirements-dev.txt # deps for dev.py (no piomatter, no truthbrush)
  .env.example

TWEET/
  FRAME1.png           # "NEW TRUTH" alert, frame 1 (64x32 RGB)
  FRAME2.png           # "NEW TRUTH" alert, frame 2 (64x32 RGB)
  FRAMES.psd           # Photoshop source for the alert frames

Pulley 3D Print/       # mechanical assets (STL/SLDPRT/SCAD)
README.md
.gitignore
```

Everything above is tracked in git except what `.gitignore` excludes:
`.env` (secrets), `__pycache__/`, `bbf-venv/`, the PIL cache built from the
BDF font on first run, and editor/OS cruft.

## Hardware

```
                                   +-------------------+
                                   |   64x32 RGB LED   |
                                   |   Matrix (HUB75)  |
                                   +---------+---------+
                                             |
                                   +---------+---------+
                                   |  Adafruit RGB     |
                                   |  Matrix Bonnet    |
                                   +---------+---------+
                                             |
                                   +---------+---------+
                                   |   Raspberry Pi 5  |
                                   +---------+---------+
                                             |  USB (serial)
                                             |
                                   +---------+---------+
                                   |   Arduino Uno     |
                                   +----+----+----+----+
                                        |    |    |
                                 H-bridge   TM1637 TM1637
                                        |    |    |
                                     Motor  Seg1  Seg2
                                            (flag %) (countdown)
```

### Arduino Uno pin map

See [arduino/flagpole/flagpole.ino](arduino/flagpole/flagpole.ino).

| Pin | Role                                 |
| --- | ------------------------------------ |
| 9   | Motor H-bridge IN1 (PWM)             |
| 10  | Motor H-bridge IN2 (PWM)             |
| 2   | Seg1 (HW-069 flag %) CLK             |
| 3   | Seg1 (HW-069 flag %) DIO             |
| 4   | Seg2 (HW-069 countdown) CLK          |
| 5   | Seg2 (HW-069 countdown) DIO          |

## Arduino firmware (`arduino/`)

1. Install the [TM1637](https://github.com/avishorp/TM1637) library by
   Avishay Orpaz via the Arduino IDE Library Manager.
2. Open `arduino/flagpole/flagpole.ino`, select the Uno + its serial port,
   and upload.
3. The firmware keeps both 7-seg modules showing dashes until the Pi sends
   its first `P` / `T` command.

Calibration data lives in the Uno's EEPROM so it survives reboots and
firmware re-flashes (EEPROM layout is preserved from the original sketch).

## Pi setup (`pi/`)

Target: Raspberry Pi 5 running Raspberry Pi OS Bookworm 64-bit.

This repo is expected to be cloned at `/home/teddyh/bigbeautifulflag/` with
a virtualenv named `bbf-venv/` inside the `pi/` subfolder. If you put it
somewhere else (or name the venv differently), edit
[`pi/systemd/flagpole.service`](pi/systemd/flagpole.service) to match before
installing the service unit.

1. Clone this repo to `/home/teddyh/bigbeautifulflag` (so the Python code
   lives at `/home/teddyh/bigbeautifulflag/pi`).
2. Create the virtualenv **inside `pi/`** and install deps:

   ```bash
   cd ~/bigbeautifulflag/pi
   python3 -m venv bbf-venv
   bbf-venv/bin/pip install -r requirements.txt
   ```

   Piomatter on Pi 5 is a separate wheel; if `pip` can't find it, follow the
   [Adafruit Pi 5 Piomatter guide](https://learn.adafruit.com/rgb-matrix-panels-with-raspberry-pi-5).

3. Wire the Adafruit RGB Matrix Bonnet onto the Pi's 40-pin header and plug
   in the 64x32 HUB75 panel. Power the panel from its own 5V supply.
4. Plug the Arduino Uno into the Pi over USB (typically appears as
   `/dev/ttyACM0`).

## Calibration

Run this once to set the motor's LOW/HIGH endpoints. The Uno persists the
result in EEPROM, so you only redo it after mechanical changes.

```bash
# First run (before the service is installed) — nothing to stop.
cd ~/bigbeautifulflag/pi
bbf-venv/bin/python calibrate.py --port /dev/ttyACM0
```

After the systemd unit is installed, the service owns the serial port
exclusively, so stop it first and start it again when done:

```bash
sudo systemctl stop flagpole.service
cd ~/bigbeautifulflag/pi
bbf-venv/bin/python calibrate.py --port /dev/ttyACM0
sudo systemctl start flagpole.service
```

Keys:

| Key        | Action                                |
| ---------- | ------------------------------------- |
| ↑ / ↓      | Fine jog (100 ms)                     |
| W / S      | Coarse jog (1 s)                      |
| L          | Mark the LOW endpoint (0%)            |
| H          | Mark the HIGH endpoint (100%)         |
| R          | Erase calibration from EEPROM         |
| ?          | Print Arduino status                  |
| 0–9        | Goto 0%, 10%, … 90% (once calibrated) |
| Q / Esc    | Quit                                  |

## Dev / demo mode

[`pi/dev.py`](pi/dev.py) runs the full service (matrix scroller + fetch loop +
tick loop + flag-percent math) with three in-process fakes swapped in for the
hardware-touching parts — no Pi, no Arduino, no Truth Social account needed.
It serves a small browser UI on `http://localhost:8765/` that shows:

- The 64×32 matrix scaled up (rendered from the same numpy framebuffer the
  real Piomatter backend would paint).
- A flagpole graphic animating up/down as the motor percent changes.
- The two 7-seg displays (flag %, next-step countdown).
- A live log of the `G` / `P` / `T` commands the service would send the Uno.
- A form to "post" a new truth, with an "Age (min)" field so you can jump
  straight to e.g. 100% without waiting 5h30m, and a "Rewind 30 min" button
  that bumps the flag up one step instantly.

```bash
cd pi
python3 -m venv bbf-venv
bbf-venv/bin/pip install -r requirements-dev.txt
bbf-venv/bin/python dev.py           # opens http://localhost:8765/
bbf-venv/bin/python dev.py --help    # --port / --host / --no-browser / --interval
```

None of the dev fakes touch the network or `/dev/ttyACM0`, so this runs fine
on macOS / Linux / Windows. The fetch loop ticks at ~3 Hz and the motor
ticker at 10 Hz (both are 1 Hz / 60 s on the real Pi) so clicks in the UI
show up effectively instantly.

The "NEW TRUTH" alert frames are synthesized in-process when the real
`TWEET/FRAME1.png` / `FRAME2.png` aren't present, so the alert animation still
plays end-to-end in dev.

## systemd

Install the service so the flagpole starts on boot and restarts on crash.

```bash
cd ~/bigbeautifulflag

# Secrets (not tracked in git)
sudo cp pi/.env.example /etc/flagpole.env
sudo chmod 600 /etc/flagpole.env
sudo $EDITOR /etc/flagpole.env    # fill in TRUTHSOCIAL_USERNAME/PASSWORD

# Service unit
sudo cp pi/systemd/flagpole.service /etc/systemd/system/flagpole.service
sudo systemctl daemon-reload
sudo systemctl enable --now flagpole.service
journalctl -u flagpole.service -f
```

Useful service commands once it's installed:

```bash
sudo systemctl stop    flagpole.service   # stop (e.g. before calibrating)
sudo systemctl start   flagpole.service   # start again
sudo systemctl restart flagpole.service   # after editing code or the unit file
sudo systemctl status  flagpole.service   # quick health check
journalctl -u flagpole.service -n 100     # recent logs
```

The unit runs as `root` because Piomatter needs raw PIO access on the Pi 5.
If you prefer a non-root user, grant `cap_sys_rawio+ep` to the python
binary and change the `User=` line in `pi/systemd/flagpole.service`.

## Configuration

All configurable via environment variables (see [pi/.env.example](pi/.env.example)):

| Var                    | Default             | Notes                                       |
| ---------------------- | ------------------- | ------------------------------------------- |
| `ARDUINO_PORT`         | `/dev/ttyACM0`      | USB-serial device for the Uno               |
| `TRUTH_HANDLE`         | `realdonaldtrump`   | Account to watch                            |
| `POLL_INTERVAL`        | `60`                | Seconds between Truth Social fetches        |
| `TRUTHSOCIAL_USERNAME` | — (required)        | Or set `TRUTHSOCIAL_TOKEN` instead          |
| `TRUTHSOCIAL_PASSWORD` | — (required)        |                                             |
| `TRUTHSOCIAL_TOKEN`    | —                   | Bearer token alternative                    |
| `LOG_LEVEL`            | `INFO`              | Any `logging` level name                    |

## Serial protocol (Pi → Uno)

9600 baud, newline-terminated, ASCII:

| Command   | Meaning                                                |
| --------- | ------------------------------------------------------ |
| `G<n>`    | Move motor to n% (0–100)                               |
| `P<n>`    | Show n on Seg1 flag display (`-1` blanks it)           |
| `T<n>`    | Start countdown of n seconds on Seg2 (`-1` blanks it)  |
| `U`/`D`   | Fine jog up/down                                       |
| `UU`/`DD` | Coarse jog up/down                                     |
| `L`/`H`   | Mark LOW / HIGH calibration endpoints                  |
| `R`       | Reset calibration                                      |
| `S`       | Print `STATUS:` line                                   |
| `?`       | Print help                                             |

The Uno's countdown ticks locally from the last `T` command, so the Pi only
needs to resend when the target changes (in practice, once per
`POLL_INTERVAL`).

## Font

[`pi/fonts/tom-thumb.bdf`](pi/fonts/tom-thumb.bdf) is the MIT-licensed
*Fixed4x6* (tom-thumb) bitmap font by Brian Swetland. It's converted to
PIL's native `.pil`/`.pbm` cache at first run (both gitignored); delete
those files if you ever replace the BDF.

## NEW TRUTH alert frames

[`TWEET/FRAME1.png`](TWEET/FRAME1.png) and
[`TWEET/FRAME2.png`](TWEET/FRAME2.png) are the two 64x32 RGB frames that
alternate every 200 ms for 5 seconds whenever a new post is detected, just
before the body scrolls in.  Replace them with any other pair of 64x32
PNGs to customise the alert — they're loaded once at service start. If the
files are missing the service still runs, it just skips the animation.
