#!/usr/bin/env python3
"""PTY recorder: spawn a CLI agent under a pseudo-terminal, drive it, write asciicast v2.

Usage:
  python3 record.py --out /path/take1.cast --cols 100 --rows 16 \
      --cwd /tmp/scratch-repo --prompt "fix the bug and run the test" \
      [--bin claude] [--arg --permission-mode --arg acceptEdits ...]

Design notes (spike for the Remotion hero pipeline):
- Timestamps every raw output chunk relative to spawn; writes NDJSON asciicast v2.
- Drives input by watching the ANSI-stripped output stream for sentinels:
    * composer ready: "? for shortcuts" / prompt glyph
    * working:        "esc to interrupt"
    * done:           "esc to interrupt" absent from the stream for QUIET_SECS
- Exits via "/exit", then Ctrl+C x2, then SIGTERM/SIGKILL.
- Writes a metadata sidecar JSON next to the cast.
"""

import argparse
import codecs
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime, timezone

ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[A-Za-z]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-9A-Za-z]"          # charset
    r"|\x1b[=>]"                     # keypad modes
    r"|[\x00-\x08\x0b-\x1f\x7f]"     # other C0
)

# NOTE: ink paints runs of spaces as cursor-forward moves, so the ANSI-stripped
# stream has NO spaces ("Quicksafetycheck:Isthisaproject..."). All sentinels are
# therefore matched space-free against a normalized tail.
COMPOSER_SENTINELS = ["?forshortcuts"]
WORKING_SENTINEL = "esctointerrupt"
TRUST_SENTINELS = ["trustthisfolder", "quicksafetycheck"]
PERMISSION_SENTINEL = "doyouwant"

QUIET_SECS = 10.0          # stream must be free of WORKING_SENTINEL this long
COMPOSER_TIMEOUT = 90.0
WORKING_TIMEOUT = 90.0
DONE_TIMEOUT = 240.0       # hard bound on task wait
EXIT_TIMEOUT = 20.0


class Recorder:
    def __init__(self, out_path, cols, rows, cwd, argv, env):
        self.out_path = out_path
        self.cols, self.rows = cols, rows
        self.cwd, self.argv, self.env = cwd, argv, env
        self.f = open(out_path, "w", encoding="utf-8")
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.tail = ""                 # ANSI-stripped rolling tail for sentinels
        self.t0 = None
        self.last_output_at = 0.0
        self.last_working_at = None    # last time WORKING_SENTINEL was seen
        self.saw_working = False
        self.phase_log = []
        self.master = None
        self.proc = None

    def log(self, msg):
        t = 0.0 if self.t0 is None else time.monotonic() - self.t0
        entry = f"[{t:8.3f}] {msg}"
        print(entry, file=sys.stderr, flush=True)
        self.phase_log.append(entry)

    def start(self):
        master, slave = os.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))
        self.master = master

        def preexec():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.t0 = time.monotonic()
        header = {
            "version": 2,
            "width": self.cols,
            "height": self.rows,
            "timestamp": int(time.time()),
            "env": {"TERM": self.env.get("TERM", ""), "SHELL": self.env.get("SHELL", "")},
            "title": " ".join(self.argv),
        }
        self.f.write(json.dumps(header) + "\n")
        self.proc = subprocess.Popen(
            self.argv, stdin=slave, stdout=slave, stderr=slave,
            cwd=self.cwd, env=self.env, preexec_fn=preexec, close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        self.log(f"spawned pid={self.proc.pid} argv={self.argv} {self.cols}x{self.rows}")

    def _drain(self, timeout=0.05):
        """Read available output, emit cast events, update sentinel tail."""
        r, _, _ = select.select([self.master], [], [], timeout)
        if self.master not in r:
            return False
        try:
            data = os.read(self.master, 65536)
        except OSError:
            return False
        if not data:
            return False
        t = time.monotonic() - self.t0
        text = self.decoder.decode(data)
        if text:
            self.f.write(json.dumps([round(t, 6), "o", text],
                                    ensure_ascii=False) + "\n")
            self.last_output_at = t
            plain = ANSI_RE.sub("", text)
            self.tail = (self.tail + plain)[-6000:]
            if WORKING_SENTINEL in self.norm_tail():
                self.last_working_at = t
                self.saw_working = True
        return True

    def norm_tail(self):
        return self.tail.lower().replace(" ", "")

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._drain(0.05)

    def send(self, s, cps_delay=0.03):
        """Type characters with a small delay, draining output as we go."""
        for ch in s:
            if not self.write_input(ch.encode("utf-8")):
                return
            self._drain(cps_delay)

    def write_input(self, data):
        try:
            os.write(self.master, data)
            return True
        except OSError:
            self.log("input write failed (process gone?)")
            return False

    def wait_for(self, predicate, timeout, label):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            self._drain(0.1)
            if self.proc.poll() is not None:
                self.log(f"process exited during wait_for({label})")
                return False
            self._handle_dialogs()
            if predicate():
                self.log(f"sentinel hit: {label}")
                return True
        self.log(f"TIMEOUT waiting for {label} ({timeout}s)")
        return False

    def _handle_dialogs(self):
        norm = self.norm_tail()
        if any(s in norm for s in TRUST_SENTINELS):
            self.log("trust dialog detected -> sending Enter")
            self.tail = ""
            self.write_input(b"\r")
            self.pump(1.5)

    def run_session(self, prompt):
        # Phase 1: composer ready
        ok = self.wait_for(
            lambda: any(s in self.norm_tail() for s in COMPOSER_SENTINELS),
            COMPOSER_TIMEOUT, "composer-ready")
        self.pump(2.0)  # let the UI settle for the recording

        # Phase 2: type the task
        self.log(f"typing prompt: {prompt!r}")
        self.send(prompt, cps_delay=0.035)
        self.pump(0.8)
        self.write_input(b"\r")
        self.log("prompt submitted")

        # Phase 3: agent starts working
        self.wait_for(lambda: self.saw_working, WORKING_TIMEOUT, "working-spinner")

        # Phase 4: done = spinner text absent from stream for QUIET_SECS
        def done():
            now = time.monotonic() - self.t0
            if not self.saw_working or self.last_working_at is None:
                return False
            quiet = now - self.last_working_at
            if quiet > QUIET_SECS:
                if PERMISSION_SENTINEL in self.norm_tail() and "❯" in self.tail[-2000:]:
                    self.log("possible permission dialog at quiet point -> Enter")
                    self.tail = ""
                    self.write_input(b"\r")
                    return False
                return True
            return False

        self.wait_for(done, DONE_TIMEOUT, "task-complete")
        self.pump(2.0)
        return ok

    def shutdown(self):
        if self.proc.poll() is None:
            self.log("sending /exit")
            self.send("/exit", cps_delay=0.05)
            self.pump(0.6)
            self.write_input(b"\r")
            end = time.monotonic() + EXIT_TIMEOUT
            while time.monotonic() < end and self.proc.poll() is None:
                self._drain(0.2)
        if self.proc.poll() is None:
            self.log("still alive -> Ctrl+C x2")
            for _ in range(2):
                if not self.write_input(b"\x03"):
                    break
                self.pump(0.6)
            end = time.monotonic() + 5
            while time.monotonic() < end and self.proc.poll() is None:
                self._drain(0.2)
        if self.proc.poll() is None:
            self.log("still alive -> SIGTERM")
            self.proc.terminate()
            time.sleep(2)
        if self.proc.poll() is None:
            self.log("still alive -> SIGKILL")
            self.proc.kill()
        # final drain
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if not self._drain(0.2):
                break
        rc = self.proc.wait()
        self.log(f"process exit status: {rc}")
        self.f.close()
        return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--rows", type=int, default=16)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--bin", default="claude")
    ap.add_argument("--arg", action="append", default=[],
                    help="extra argv entries for the binary (repeatable)")
    ap.add_argument("--raw-command", action="store_true",
                    help="run the binary without agent driving; just record until exit")
    args = ap.parse_args()

    env = dict(os.environ)
    # Scrub nested-session and noise vars.
    for k in list(env):
        if k.startswith(("CLAUDE", "LONGHOUSE", "MCP_", "GIT_")) or k == "ANTHROPIC_MODEL":
            del env[k]
    env["TERM"] = "xterm-256color"
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["COLUMNS"] = str(args.cols)
    env["LINES"] = str(args.rows)

    argv = [args.bin] + args.arg
    rec = Recorder(args.out, args.cols, args.rows, args.cwd, argv, env)
    rec.start()
    if args.raw_command:
        end = time.monotonic() + DONE_TIMEOUT
        while time.monotonic() < end and rec.proc.poll() is None:
            rec._drain(0.1)
    else:
        rec.run_session(args.prompt)
    rc = rec.shutdown()

    sha = hashlib.sha256(open(args.out, "rb").read()).hexdigest()
    version = ""
    try:
        version = subprocess.run([args.bin, "--version"], capture_output=True,
                                 text=True, timeout=20).stdout.strip()
    except Exception as e:  # noqa: BLE001
        version = f"unavailable: {e}"
    meta = {
        "binary": args.bin,
        "binary_version": version,
        "argv": argv,
        "prompt": args.prompt,
        "cols": args.cols,
        "rows": args.rows,
        "term": env["TERM"],
        "cwd": args.cwd,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cast_sha256": sha,
        "exit_status": rc,
        "phase_log": rec.phase_log,
    }
    meta_path = os.path.splitext(args.out)[0] + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"wrote {args.out} (sha256 {sha[:12]}...) and {meta_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
