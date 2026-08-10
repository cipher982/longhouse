#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""PTY recorder: spawn a CLI agent under a pseudo-terminal, drive it, write asciicast v2.

Two modes:

V1 (host binary, operator env — legacy spike mode):
  uv run record.py --out take1.cast --cwd /tmp/scratch --prompt "..." --bin claude

V2 (hermetic sandbox — the factory mode):
  uv run record.py --sandbox --provider claude --out take1.cast
  * binary comes from .sandbox/<provider>/<version>/ (see fetch.py)
  * HOME is a fresh mktemp dir; env is an explicit allowlist + ONE auth var
    resolved from Infisical at record time
  * cwd is a freshly seeded per-run scratch repo (mkdtemp parent, leaf name
    kept as "demo-repo" because the provider prints its cwd)
  * srt isolation is PROVEN by a canary before recording; after recording,
    fail-closed gates make a broken take exit nonzero (see meta "gates")
  * sentinel profile (ready / dialogs / working / exit) comes from providers.yml

Design notes:
- Timestamps every raw output chunk relative to spawn; writes NDJSON asciicast v2.
- Sentinels match an ANSI-stripped, lowercased, whitespace-removed rolling tail
  (ink paints runs of spaces as cursor-forward moves, so the stripped stream has
  no spaces; NBSP is also normalized away).
- Exits via the profile's exit sequence, then Ctrl+C x2, then SIGTERM/SIGKILL.
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
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[A-Za-z]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-9A-Za-z]"          # charset
    r"|\x1b[=>]"                     # keypad modes
    r"|[\x00-\x08\x0b-\x1f\x7f]"     # other C0
)

# Terminal queries the recorded TUIs emit at startup. A real terminal answers
# these on stdin; if nobody answers, Claude Code's ink UI sits waiting and
# early keystrokes are swallowed by the pending-response reader (observed in
# claude take2: the theme-dialog Enter at t=0.4s did nothing and the screen
# froze for 60s). The recorder answers like a plain xterm.
TERM_QUERIES = [
    (re.compile(rb"\x1b\[6n"), b"\x1b[1;1R"),                         # CPR
    (re.compile(rb"\x1b\[>0?q"), b"\x1bP>|xterm(377)\x1b\\"),         # XTVERSION
    (re.compile(rb"\x1b\[\?u"), b"\x1b[?0u"),                          # kitty kbd
    (re.compile(rb"\x1b\[>0?c"), b"\x1b[>41;377;0c"),                  # DA2
    (re.compile(rb"\x1b\[0?c"), b"\x1b[?62;22c"),                      # DA1
    (re.compile(rb"\x1b\]10;\?(?:\x07|\x1b\\)"), b"\x1b]10;rgb:e5e5/e5e5/e5e5\x1b\\"),
    (re.compile(rb"\x1b\]11;\?(?:\x07|\x1b\\)"), b"\x1b]11;rgb:1212/1212/1212\x1b\\"),
]
DECRQM_RE = re.compile(rb"\x1b\[\?(\d+)\$p")

KEY_MAP = {
    "enter": "\r",
    "tab": "\t",
    "space": " ",
    "esc": "\x1b",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "ctrl-c": "\x03",
}

# V1 Claude profile, kept as the default for legacy (non-sandbox) invocations.
DEFAULT_PROFILE = {
    "ready": [],
    "ready_raw": "❯ ",  # prompt glyph + NBSP
    "dialogs": [
        {"id": "trust", "match": ["trustthisfolder", "quicksafetycheck"], "keys": ["enter"]},
        {"id": "permission", "match": ["doyouwant"], "keys": ["enter"]},
    ],
    "working_regex": r"esctointerrupt|\(\d+s",
    "exit": ["text:/exit", "enter"],
}

DEFAULTS = {
    "quiet_secs": 10.0,
    "composer_timeout": 60.0,
    "working_timeout": 60.0,
    "done_timeout": 240.0,
}
EXIT_TIMEOUT = 20.0

INVENTORY_PY = '''"""Tiny inventory helpers for the demo shop."""


def count_items(shelves):
    """Count every item across all shelves.

    Each shelf is a list of item names.
    """
    total = 0
    for shelf in shelves[1:]:
        total += len(shelf)
    return total


def busiest_shelf(shelves):
    """Return the index of the shelf with the most items."""
    best = 0
    for i, shelf in enumerate(shelves):
        if len(shelf) > len(shelves[best]):
            best = i
    return best
'''

TEST_INVENTORY_PY = '''from inventory import busiest_shelf, count_items


def test_count_items():
    shelves = [["apple", "pear"], ["soap"], ["nail", "screw", "bolt"]]
    assert count_items(shelves) == 6


def test_count_items_single_shelf():
    assert count_items([["only", "shelf"]]) == 2


def test_busiest_shelf():
    shelves = [["a"], ["b", "c", "d"], ["e", "f"]]
    assert busiest_shelf(shelves) == 1


if __name__ == "__main__":
    test_count_items()
    test_count_items_single_shelf()
    test_busiest_shelf()
    print("all tests passed")
'''


def keys_to_bytes(keys):
    out = ""
    for k in keys:
        if k.startswith("text:"):
            out += k[5:]
        else:
            out += KEY_MAP[k]
    return out.encode("utf-8")


class Recorder:
    def __init__(self, out_path, cols, rows, cwd, argv, env, profile, timeouts):
        self.out_path = out_path
        self.cols, self.rows = cols, rows
        self.cwd, self.argv, self.env = cwd, argv, env
        self.profile = profile
        self.timeouts = timeouts
        self.working_re = re.compile(profile["working_regex"]) if profile.get("working_regex") else None
        self.f = open(out_path, "w", encoding="utf-8")
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.tail = ""                 # ANSI-stripped rolling tail for sentinels
        self.t0 = None
        self.last_output_at = 0.0
        self.last_working_at = None
        self.saw_working = False
        self.dialog_counts = {}
        self._pending_dialog = None
        self.ready_seen = False
        self.completion = None  # "quiet-sentinel" | "done-timeout" | None
        self.aborted = False
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
            "title": " ".join([os.path.basename(self.argv[0])] + self.argv[1:]),
        }
        self.f.write(json.dumps(header) + "\n")
        argv, env = run_isolated(self.argv, self.env, self.cwd)
        self.proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            cwd=self.cwd, env=env, preexec_fn=preexec, close_fds=True,
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
        self._answer_queries(data)
        t = time.monotonic() - self.t0
        text = self.decoder.decode(data)
        if text:
            self.f.write(json.dumps([round(t, 6), "o", text],
                                    ensure_ascii=False) + "\n")
            self.last_output_at = t
            plain = ANSI_RE.sub("", text)
            self.tail = (self.tail + plain)[-6000:]
            if self.working_re and self.working_re.search(self.norm_tail()):
                self.last_working_at = t
                self.saw_working = True
        return True

    def _answer_queries(self, data):
        """Answer terminal capability queries so the TUI treats us as a real
        xterm and its input pipeline comes up immediately."""
        for pat, resp in TERM_QUERIES:
            for _ in pat.finditer(data):
                self.write_input(resp)
        for m in DECRQM_RE.finditer(data):
            self.write_input(b"\x1b[?" + m.group(1) + b";2$y")

    def norm_tail(self):
        return (self.tail.lower()
                .replace(" ", "")
                .replace(" ", "")
                .replace(" ", "")
                .replace(" ", ""))

    def fresh_tail(self, n=800):
        # Only the most recent slice, so stale dialog text scrolled up in the
        # 6000-char window does not keep re-triggering a dialog handler.
        return self.norm_tail()[-n:]

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._drain(0.05)

    def send(self, s, cps_delay=0.03):
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

    def _send_dialog_keys(self, dlg):
        self.tail = ""
        for key in dlg["keys"]:
            self.write_input(keys_to_bytes([key]))
            self.pump(0.4)
        self.pump(dlg.get("settle", 1.5))
        self._pending_dialog = (dlg, time.monotonic(), self.last_output_at)

    def _handle_dialogs(self):
        # Once the composer is up, dialogs are behind us; stop matching stale
        # onboarding text and stop the swallowed-keystroke retry heuristic
        # (a legitimately idle composer produces no output and would misfire).
        if self._composer_ready():
            self.ready_seen = True
        if self.ready_seen:
            self._pending_dialog = None
            return
        # Retry a dialog answer that produced no output (keystroke swallowed).
        if self._pending_dialog:
            dlg, sent_at, out_at = self._pending_dialog
            if time.monotonic() - sent_at > 2.5:
                if self.last_output_at <= out_at + 1e-6 and self.proc.poll() is None:
                    n = self.dialog_counts.get(dlg["id"], 0)
                    if n < dlg.get("max_times", 4):
                        self.dialog_counts[dlg["id"]] = n + 1
                        self.log(f"dialog {dlg['id']!r} answer produced no output -> resending")
                        self._send_dialog_keys(dlg)
                        return
                self._pending_dialog = None
        fresh = self.fresh_tail()
        for dlg in self.profile.get("dialogs", []):
            if any(s in fresh for s in dlg["match"]):
                n = self.dialog_counts.get(dlg["id"], 0)
                if n >= dlg.get("max_times", 4):
                    continue
                self.dialog_counts[dlg["id"]] = n + 1
                self.log(f"dialog {dlg['id']!r} detected -> keys {dlg['keys']}")
                self._send_dialog_keys(dlg)
                return

    def _composer_ready(self):
        norm = self.norm_tail()
        if any(s in norm for s in self.profile.get("ready", [])):
            return True
        raw = self.profile.get("ready_raw")
        if raw and raw in self.tail:
            return True
        return False

    def run_session(self, prompt, require_ready=False):
        tmo = self.timeouts
        # Phase 1: composer ready
        ok = self.wait_for(self._composer_ready, tmo["composer_timeout"], "composer-ready")
        if ok:
            self.ready_seen = True
        if not ok and require_ready:
            # Never type the prompt into an unknown screen (take2: the prompt
            # landed in an onboarding dialog and confirmed its default).
            self.log("ABORT: composer never ready; refusing to type into unknown screen")
            self.aborted = True
            return False
        self.pump(2.0)  # let the UI settle for the recording
        # A dialog may have appeared during the settle (e.g. trust after theme).
        self._handle_dialogs()

        # Phase 2: type the task
        self.log(f"typing prompt: {prompt!r}")
        self.send(prompt, cps_delay=0.035)
        self.pump(0.8)
        self.write_input(b"\r")
        self.log("prompt submitted")

        # Phase 3: agent starts working
        self.wait_for(lambda: self.saw_working, tmo["working_timeout"], "working-spinner")

        # Phase 4: done = working pattern absent from stream for quiet_secs
        def done():
            now = time.monotonic() - self.t0
            if not self.saw_working or self.last_working_at is None:
                return False
            return (now - self.last_working_at) > tmo["quiet_secs"]

        # Both outcomes are recorded so the take gates can name which path
        # completed the take: the quiet-period sentinel, or the bounded
        # done_timeout (still a defined completion, just the slow lane).
        if self.wait_for(done, tmo["done_timeout"], "task-complete"):
            self.completion = "quiet-sentinel"
        elif self.saw_working:
            self.completion = "done-timeout"
        self.pump(2.0)
        return ok

    def shutdown(self):
        exit_keys = self.profile.get("exit", ["text:/exit", "enter"])
        if self.proc.poll() is None:
            self.log(f"sending exit sequence {exit_keys}")
            for key in exit_keys:
                if key.startswith("text:"):
                    self.send(key[5:], cps_delay=0.05)
                else:
                    self.write_input(keys_to_bytes([key]))
                self.pump(0.6)
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
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if not self._drain(0.2):
                break
        rc = self.proc.wait()
        self.log(f"process exit status: {rc}")
        self.f.close()
        return rc


# ---------------------------------------------------------------- sandbox mode

BROWSER_SHIM = """#!/bin/sh
# Browser-suppression shim: log the attempt, do nothing, succeed silently.
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) {name} $@" >> "$HOME/.browser-attempts.log"
exit 0
"""


SRT_WRAP = None  # set by main(): {"bin": <srt path>, "settings": <json path>}


def run_isolated(argv, env, cwd):
    """Isolation seam. When SRT_WRAP is set, the whole agent process runs under
    @anthropic-ai/sandbox-runtime (seatbelt on macOS): deny-by-default network
    (loopback mock only), denyRead on the operator's /Users, allowPty on,
    allowAppleEvents off (the browser/OAuth blocker). The PATH shims +
    BROWSER=false stay as telemetry; srt is the enforcement layer."""
    if SRT_WRAP:
        # "--" so srt never re-parses provider flags (e.g. a "-s") as its own.
        return [SRT_WRAP["bin"], "-s", SRT_WRAP["settings"], "--", *argv], env
    return argv, env


def write_srt_settings(home, script_dir, mock_url=None, extra_domains=()):
    """Per-recording srt settings, generated fresh into the sandbox HOME."""
    allowed = list(extra_domains)
    if mock_url:
        port = mock_url.rsplit(":", 1)[1]
        allowed += [f"localhost:{port}", f"127.0.0.1:{port}"]
    settings = {
        "network": {
            "allowedDomains": allowed,
            "deniedDomains": [],
            # Grants outbound to loopback only (seatbelt: remote ip
            # "localhost:*"); the domain allowlist still governs proxied egress.
            "allowLocalBinding": True,
        },
        "filesystem": {
            # Real operator files are unreadable; only the fetched pinned
            # binaries are re-allowed underneath /Users.
            "denyRead": ["/Users"],
            "allowRead": [str(script_dir / ".sandbox")],
            # /var/folders is macOS TMPDIR: without it, python inside the
            # sandbox emits "couldn't create cache file" noise into the take.
            "allowWrite": [home, "/tmp", "/private/tmp", "/private/var/folders", "/dev/null"],
            "denyWrite": [],
        },
        "allowPty": True,
        "allowAppleEvents": False,
    }
    path = os.path.join(home, "srt-settings.json")
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    return path


def find_srt(manifest):
    rt = (manifest.get("runtime") or {}).get("srt")
    if not rt:
        return None
    p = SCRIPT_DIR / ".sandbox" / "srt" / rt["source"]["version"] / rt["bin"]
    return str(p) if p.exists() else None


def run_canary(env, srt_wrap, mock_url, extra_domains):
    """Prove the srt sandbox is actually enforcing before anything records.

    Isolation is asserted by the settings file; this runs cheap probes under
    the SAME srt binary + settings the take will use and refuses to record if
    any expectation fails:
      (a) egress is blocked (direct IP; plus a non-allowlisted domain when the
          provider needs preflight hosts allowlisted, e.g. Claude Code)
      (b) operator files (~/.claude/settings.json) are unreadable
      (c) the loopback mock health endpoint IS reachable (when mock lane)
    Returns the results dict for the take's meta sidecar."""

    def srt_run(cmd):
        # "--" is load-bearing: srt (commander) would otherwise re-parse a
        # later "-s"/"-v" from the probe command as its OWN flags.
        return subprocess.run(
            [srt_wrap["bin"], "-s", srt_wrap["settings"], "--", *cmd],
            env=env, capture_output=True, text=True, timeout=60)

    def curl_probe(url):
        """Returns (exit, http_code, proxy_blocked). srt's filtering proxy
        answers plain-http denials with 403 + X-Proxy-Error:
        blocked-by-allowlist (https CONNECT is refused outright: exit 56)."""
        r = srt_run(["curl", "-s", "-D", "-", "-o", "/dev/null",
                     "-w", "\\n%{http_code}", "--max-time", "3", url])
        out = (r.stdout or "").strip()
        code = out.rsplit("\n", 1)[-1].strip() if out else ""
        proxy_blocked = "x-proxy-error: blocked-by-allowlist" in out.lower()
        return r.returncode, code, proxy_blocked

    def egress_blocked(rc, code, proxy_blocked):
        return rc != 0 or code == "000" or (code == "403" and proxy_blocked)

    results = {}
    failures = []

    for url in ("http://1.1.1.1", "https://1.1.1.1"):
        rc, code, pb = curl_probe(url)
        blocked = egress_blocked(rc, code, pb)
        key = "egress_ip_http" if url.startswith("http:") else "egress_ip_https"
        results[key] = {"url": url, "exit": rc, "http_code": code,
                        "proxy_blocked": pb, "blocked": blocked}
        if not blocked:
            failures.append(f"{key}: curl {url} escaped the sandbox "
                            f"(exit {rc}, http {code})")

    if extra_domains:
        # Provider allowlists real hosts (Claude Code's mandatory preflight);
        # prove a host OUTSIDE that allowlist is still blocked.
        rc, code, pb = curl_probe("https://example.com")
        blocked = egress_blocked(rc, code, pb)
        results["egress_nonallowlisted"] = {
            "url": "https://example.com", "exit": rc, "http_code": code,
            "proxy_blocked": pb, "blocked": blocked}
        if not blocked:
            failures.append(f"egress_nonallowlisted: curl https://example.com "
                            f"escaped the sandbox (exit {rc}, http {code})")

    # Path stored tilde-form so the meta sidecar never carries /Users/<name>.
    probe = os.path.expanduser("~/.claude/settings.json")
    if os.path.exists(probe):
        r = srt_run(["cat", probe])
        denied = r.returncode != 0 and not r.stdout.strip()
        results["operator_read"] = {"path": "~/.claude/settings.json",
                                    "exit": r.returncode, "denied": denied}
    else:
        r = srt_run(["ls", os.path.expanduser("~")])
        denied = r.returncode != 0
        results["operator_read"] = {"path": "~", "exit": r.returncode,
                                    "denied": denied}
    if not denied:
        failures.append("operator_read: sandbox can read operator files "
                        f"({results['operator_read']['path']})")

    if mock_url:
        rc, code, pb = curl_probe(mock_url + "/health")
        reachable = rc == 0 and code == "200" and not pb
        results["mock_health"] = {"url": mock_url + "/health", "exit": rc,
                                  "http_code": code, "reachable": reachable}
        if not reachable:
            failures.append(f"mock_health: loopback mock unreachable from "
                            f"sandbox (exit {rc}, http {code})")

    results["passed"] = not failures
    for f in failures:
        print(f"CANARY FAILED: {f}", file=sys.stderr)
    if failures:
        raise SystemExit(
            "canary failed: srt isolation not proven; refusing to record")
    print(f"canary passed: {json.dumps(results)}", file=sys.stderr)
    return results


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_mock(fixture_path, home, demo_repo=None):
    """Start mock_llm.py; returns (proc, base_url)."""
    import urllib.request
    port = free_port()
    log = os.path.join(home, ".mock-requests.ndjson")
    argv = [sys.executable, str(SCRIPT_DIR / "mock_llm.py"),
            "--port", str(port), "--fixture", str(SCRIPT_DIR / fixture_path),
            "--log", log]
    if demo_repo:
        argv += ["--demo-repo", demo_repo]
    proc = subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            with urllib.request.urlopen(base + "/health", timeout=1):
                return proc, base
        except Exception:  # noqa: BLE001
            if proc.poll() is not None:
                raise SystemExit("mock_llm.py died on startup")
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("mock_llm.py never became healthy")


def load_manifest():
    import yaml
    return yaml.safe_load((SCRIPT_DIR / "providers.yml").read_text())


def resolve_secret(secret):
    """Fetch ONE secret value via infisical-get.py. Runs with the operator env
    (Infisical machine token), but the value is the only thing that crosses
    into the sandbox."""
    script = os.path.expanduser("~/git/me/scripts/infisical-get.py")
    argv = ["python3", script, secret["key"]]
    if secret.get("project"):
        argv += ["--project", secret["project"]]
    if secret.get("env"):
        argv += ["--env", secret["env"]]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"failed to resolve secret {secret['key']}: {out.stderr.strip()}")
    return out.stdout.strip()


def seed_demo_repo(parent_dir="/tmp"):
    """Fresh scratch repo with an off-by-one bug, at a per-run path.

    The provider prints its cwd on screen, so the path must be the clean
    fixed name /tmp/demo-repo, and it must be a REAL directory: Claude
    binds its folder-trust grant to the resolved path, so a symlinked cwd
    resurfaces the per-edit permission dialog mid-task (observed: take
    stalled on "Do you want to make this edit" and failed the mock gate).
    Reuse of the fixed path is guarded by a marker inside .git so we
    never remove a directory we did not seed.
    Returns (repo_path, scratch_parent) — caller removes scratch_parent."""
    path = os.path.join(parent_dir, "demo-repo")
    marker = os.path.join(path, ".git", "longhouse-demo-seed")
    if os.path.islink(path):
        os.unlink(path)  # leftover from the abandoned symlink scheme
    elif os.path.isdir(path):
        if not os.path.exists(marker):
            raise SystemExit(f"{path} exists but has no seed marker — refusing to remove it")
        shutil.rmtree(path)
    elif os.path.exists(path):
        raise SystemExit(f"{path} exists and is not a directory — refusing to touch it")
    os.makedirs(path)
    scratch = path
    display = path
    Path(path, "inventory.py").write_text(INVENTORY_PY)
    Path(path, "test_inventory.py").write_text(TEST_INVENTORY_PY)
    git = "/usr/bin/git"
    env = {"HOME": path, "PATH": "/usr/bin:/bin",
           "GIT_CONFIG_NOSYSTEM": "1"}
    def g(*args):
        subprocess.run([git, "-C", path, *args], check=True, env=env,
                       capture_output=True)
    g("init", "-q")
    g("config", "user.name", "Demo")
    g("config", "user.email", "demo@example.com")
    g("add", "inventory.py", "test_inventory.py")
    g("commit", "-q", "-m", "seed demo repo")
    Path(path, ".git", "longhouse-demo-seed").write_text("recorder scratch\n")
    return display, scratch




def _unlink_demo_symlink():
    """Remove the fixed-name /tmp/demo-repo display symlink if it is ours."""
    display = "/tmp/demo-repo"
    if os.path.islink(display):
        os.unlink(display)



def stage_system_roots(home):
    """Make macOS trust roots available to a sandboxed Node CLI."""
    roots = Path(home, "system-roots.pem")
    result = subprocess.run(
        ["/usr/bin/security", "find-certificate", "-a", "-p",
         "/System/Library/Keychains/SystemRootCertificates.keychain"],
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        raise SystemExit("failed to export macOS system root certificates")
    roots.write_bytes(result.stdout)
    return str(roots)


def build_sandbox(provider_name, prov, cols, rows, demo_repo=None, fixture_path=None):
    """Blank HOME + allowlisted env + PATH shim dir + auth lane.

    Returns (env, home, argv, mock_proc, mock_url, auth_mode)."""
    version = prov["source"]["version"]
    root = SCRIPT_DIR / ".sandbox" / provider_name / version
    bin_path = root / prov["bin"]
    if not bin_path.exists():
        raise SystemExit(f"binary not fetched: {bin_path} (run fetch.py {provider_name})")

    home = tempfile.mkdtemp(prefix="demo-home-", dir="/tmp")
    bindir = Path(home, "bin")
    bindir.mkdir()
    canonical = os.path.basename(prov["bin"])
    os.symlink(bin_path, bindir / canonical)
    # node is always linked: npm-distributed CLIs need it, and so does the srt
    # wrapper itself.
    node = shutil.which("node")
    if not node:
        raise SystemExit("node not found on operator PATH")
    os.symlink(node, bindir / "node")

    # Browser suppression: fake open/xdg-open/osascript log and exit 0. A
    # provider that tries to open a browser gets telemetry, not Chrome.
    for shim in ("open", "xdg-open", "osascript"):
        p = bindir / shim
        p.write_text(BROWSER_SHIM.replace("{name}", shim))
        p.chmod(0o755)

    env = {
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PATH": f"{bindir}:/usr/bin:/bin",
        "DISABLE_AUTOUPDATER": "1",
        "BROWSER": "/usr/bin/false",
        "COLUMNS": str(cols),
        "LINES": str(rows),
    }

    auth = prov.get("auth") or {}
    mode = auth.get("mode", "blocked")
    env.update({str(key): str(value) for key, value in (auth.get("extra_env") or {}).items()})
    if auth.get("system_roots"):
        roots = stage_system_roots(home)
        env.update({
            "SSL_CERT_FILE": roots,
            "NODE_EXTRA_CA_CERTS": roots,
            "CURL_CA_BUNDLE": roots,
        })
    mock_proc = None
    mock_url = None
    if mode == "mock":
        mock_proc, mock_url = start_mock(fixture_path or auth["fixture"], home, demo_repo)
        env[auth["env"]] = "sk-mock-longhouse"
        if auth.get("base_url_env"):
            env[auth["base_url_env"]] = mock_url
    elif mode == "api_key":
        env[auth["env"]] = resolve_secret(auth["secret"])
    else:
        raise SystemExit(f"provider {provider_name} auth mode is blocked; refusing to record")

    for rel, content in (prov.get("files") or {}).items():
        p = Path(home, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mock_url:
            content = content.replace("{MOCK_URL}", mock_url)
        p.write_text(content)

    argv = [str(bindir / canonical)] + list(prov.get("args") or [])
    return env, home, argv, mock_proc, mock_url, mode


def run_setup(prov, env, cwd):
    for step in prov.get("setup") or []:
        argv = list(step["argv"])
        stdin_data = None
        if step.get("stdin_env"):
            stdin_data = env[step["stdin_env"]] + "\n"
        print(f"setup: {' '.join(argv)}", file=sys.stderr)
        r = subprocess.run(argv, env=env, cwd=cwd, input=stdin_data,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(
                f"setup step failed ({r.returncode}): {' '.join(argv)}\n"
                f"stdout: {r.stdout[-2000:]}\nstderr: {r.stderr[-2000:]}")
        print(f"setup ok: {r.stdout.strip()[-200:]}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cols", type=int, default=None)
    ap.add_argument("--rows", type=int, default=None)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--bin", default="claude")
    ap.add_argument("--arg", action="append", default=[],
                    help="extra argv entries for the binary (repeatable)")
    ap.add_argument("--raw-command", action="store_true",
                    help="run the binary without agent driving; just record until exit")
    ap.add_argument("--sandbox", action="store_true",
                    help="hermetic mode: blank HOME, allowlisted env, pinned binary")
    ap.add_argument("--provider", default=None,
                    help="provider name from providers.yml (sandbox mode)")
    ap.add_argument("--fixture", default=None,
                    help="override the provider mock fixture path (sandbox mode)")
    ap.add_argument("--keep-home", action="store_true",
                    help="do not delete the sandbox HOME after recording")
    ap.add_argument("--no-srt", action="store_true",
                    help="skip the srt enforcement wrapper (debugging only)")
    args = ap.parse_args()

    canary = None
    scratch_dir = None
    if args.sandbox:
        if not args.provider:
            raise SystemExit("--sandbox requires --provider")
        manifest = load_manifest()
        defaults = manifest.get("defaults", {})
        prov = manifest["providers"][args.provider]
        cols = args.cols or defaults.get("cols", 100)
        rows = args.rows or defaults.get("rows", 16)
        prompt = args.prompt or prov.get("prompt") or defaults["prompt"]
        profile = prov["sentinels"]
        timeouts = {k: float(prov.get(k, defaults.get(k, DEFAULTS[k])))
                    for k in DEFAULTS}
        if args.cwd:
            cwd = args.cwd
        else:
            cwd, scratch_dir = seed_demo_repo()
        env, home, argv, mock_proc, mock_url, auth_mode = build_sandbox(
            args.provider, prov, cols, rows, demo_repo=cwd, fixture_path=args.fixture)
        if args.no_srt and args.provider == "cursor":
            # The manual Cursor fallback retains only the isolated HOME, shim
            # PATH, task terminal, API key, and local trust/credential inputs.
            keep = (
                "HOME", "PATH", "TERM", "COLUMNS", "LINES", "BROWSER",
                "CURSOR_API_KEY", "AGENT_CLI_CREDENTIAL_STORE",
                "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
            )
            env = {key: env[key] for key in keep if key in env}
        run_setup(prov, env, cwd)
        binary_label = f"{prov['source'].get('package', prov['source'].get('url'))}@{prov['source']['version']}"
        preflight_domains = (prov.get("auth") or {}).get("preflight_domains", [])
        srt_bin = None if args.no_srt else find_srt(manifest)
        if srt_bin:
            settings_path = write_srt_settings(
                home, SCRIPT_DIR, mock_url, extra_domains=preflight_domains)
            global SRT_WRAP
            SRT_WRAP = {"bin": srt_bin, "settings": settings_path}
            print(f"srt enforcement: {srt_bin} settings={settings_path}", file=sys.stderr)
            # Enforced canary: no recording unless isolation is PROVEN under
            # the exact srt binary + settings this take will run with.
            try:
                canary = run_canary(env, SRT_WRAP, mock_url, preflight_domains)
            except BaseException:
                if mock_proc is not None:
                    mock_proc.terminate()
                if scratch_dir:
                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    _unlink_demo_symlink()
                if not args.keep_home:
                    shutil.rmtree(home, ignore_errors=True)
                raise
        elif not args.no_srt:
            raise SystemExit("srt not fetched (run fetch.py srt) — use --no-srt to override")
    else:
        cols = args.cols or 100
        rows = args.rows or 16
        if not args.cwd or not args.prompt:
            raise SystemExit("legacy mode requires --cwd and --prompt")
        prompt = args.prompt
        profile = DEFAULT_PROFILE
        timeouts = dict(DEFAULTS)
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("CLAUDE", "LONGHOUSE", "MCP_", "GIT_")) or k == "ANTHROPIC_MODEL":
                del env[k]
        env["TERM"] = "xterm-256color"
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        env["DISABLE_AUTOUPDATER"] = "1"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        cwd = args.cwd
        home = None
        mock_proc = None
        mock_url = None
        auth_mode = "operator-env"
        argv = [args.bin] + args.arg
        binary_label = args.bin

    try:
        rec = Recorder(args.out, cols, rows, cwd, argv, env, profile, timeouts)
        rec.start()
        if args.raw_command:
            end = time.monotonic() + timeouts["done_timeout"]
            while time.monotonic() < end and rec.proc.poll() is None:
                rec._drain(0.1)
        else:
            rec.run_session(prompt, require_ready=bool(args.sandbox))
        rc = rec.shutdown()

        # Query the mock's request stats BEFORE tearing it down: the take gate
        # needs proof the fixture's completion lane was actually exercised.
        mock_stats = None
        if mock_proc is not None:
            import urllib.request
            try:
                with urllib.request.urlopen(mock_url + "/stats", timeout=5) as resp:
                    mock_stats = json.load(resp)
            except Exception as e:  # noqa: BLE001
                mock_stats = {"error": str(e)}
            mock_proc.terminate()

        sha = hashlib.sha256(open(args.out, "rb").read()).hexdigest()
        version = ""
        try:
            version = subprocess.run([argv[0], "--version"], capture_output=True,
                                     text=True, timeout=30, env=env).stdout.strip()
        except Exception as e:  # noqa: BLE001
            version = f"unavailable: {e}"

        browser_attempts = []
        mock_requests = 0
        if home:
            attempts_log = Path(home, ".browser-attempts.log")
            if attempts_log.exists():
                browser_attempts = attempts_log.read_text().strip().splitlines()
            mock_log = Path(home, ".mock-requests.ndjson")
            if mock_log.exists():
                mock_requests = len(mock_log.read_text().strip().splitlines())

        # Fail-closed take gates: a take only exits 0 when every observable
        # says the recorded story actually happened. Anything else is a broken
        # take and must not be publishable.
        gates = None
        gate_failures = []
        if args.sandbox and not args.raw_command:
            gates = {
                "exit_status_observed": rc is not None,
                "composer_ready": rec.ready_seen,
                "working_seen": rec.saw_working,
                "completion_reached": rec.completion is not None,
                "no_browser_attempts": len(browser_attempts) == 0,
            }
            if auth_mode == "mock":
                gates["mock_requests_present"] = mock_requests > 0
                # Every fixture turn must have been served on the main lane
                # (see mock_llm.py: distinct turns, not raw request count,
                # because providers add warmup/summarizer main-model calls).
                expected = (mock_stats or {}).get("expected_turns")
                gates["mock_completions_match_fixture"] = (
                    isinstance(mock_stats, dict)
                    and "error" not in mock_stats
                    and mock_stats.get("main_requests", 0) > 0
                    and expected is not None
                    and mock_stats.get("distinct_main_turns") == expected)
            gate_failures = [k for k, v in gates.items() if not v]

        meta = {
            "binary": binary_label,
            "binary_version": version,
            "argv": [os.path.basename(argv[0])] + argv[1:],
            "prompt": prompt,
            "cols": cols,
            "rows": rows,
            "term": env["TERM"],
            "cwd": cwd,
            "sandbox": bool(args.sandbox),
            "isolation": "srt" if SRT_WRAP else "env-allowlist",
            "aborted": rec.aborted,
            "auth_mode": auth_mode,
            "canary": canary,
            "browser_attempts": browser_attempts,
            "mock_requests": mock_requests,
            "mock_stats": mock_stats,
            "completion": rec.completion,
            "gates": gates,
            "gate_failures": gate_failures,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "cast_sha256": sha,
            "exit_status": rc,
            "phase_log": rec.phase_log,
        }
        meta_path = os.path.splitext(args.out)[0] + ".meta.json"
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
            f.write("\n")
        print(f"wrote {args.out} (sha256 {sha[:12]}...) and {meta_path}", file=sys.stderr)

        for g in gate_failures:
            print(f"GATE FAILED: {g}", file=sys.stderr)
        if gate_failures:
            raise SystemExit(
                f"take failed {len(gate_failures)} gate(s): "
                f"{', '.join(gate_failures)} — exiting nonzero so nothing publishes it")
    finally:
        if mock_proc is not None and mock_proc.poll() is None:
            mock_proc.terminate()
        if scratch_dir:
            shutil.rmtree(scratch_dir, ignore_errors=True)
            _unlink_demo_symlink()
        if args.sandbox and home and not args.keep_home:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
