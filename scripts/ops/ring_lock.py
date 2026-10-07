#!/usr/bin/env python3
"""Ring locks: single writer per live surface, as an expiring lock instead of a prose claim.

A live surface (a release ring, the production pointer, the laptop release) has one writer at a
time. That used to be a `brief note` on a docket item; one claim was never released and the next
agent had to read prose to decide whether it was safe. Now the scripts that write a surface take
its lock first, refuse with the holder's details while it is held, and release it in their EXIT
trap. A lock nobody can be holding any more is reclaimable, and the reclaim is logged:

  - it expired (its holder did not renew it within its TTL), or
  - its holder process is gone (same host, and that pid is dead or is now another process).

  ring_lock.py acquire SURFACE --sha SHA --ttl SECONDS --pid PID [--op TEXT]   -> prints a token
  ring_lock.py renew SURFACE --token TOKEN --ttl SECONDS [--sha SHA]
  ring_lock.py release SURFACE --token TOKEN
  ring_lock.py status [SURFACE] [--json]

Exit 0: done. Exit 1: refused (held by someone else; renew: this token no longer holds it).
Exit 2: could not decide (bad arguments, unreadable store).

The store is `<git common dir>/ring-locks/` (one JSON file per surface, `events.jsonl` beside it),
next to the review receipts every promotion already reads, so every worktree and every agent on
this machine sees the same locks. Promotion is already bound to this machine by the review gate,
which reads receipts from that same directory; the control plane separately serializes the
tenant-mutating part of a promotion (idempotency keys, supersede by source order), so a
promotion started elsewhere cannot corrupt a ring, it can only race the demo pin and the gates.
TTLs are chosen by the callers from measured durations (scripts/lib/ring-lock.sh and the
promote/release scripts say which).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
DIRNAME = "ring-locks"
SURFACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SESSION_ENV = ("LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_SESSION_ID", "LONGHOUSE_CHANNEL_SESSION_ID")
STORE_ENV = "LONGHOUSE_RING_LOCK_DIR"  # tests point this at a scratch directory


class LockError(Exception):
    pass


def store(repo: str) -> Path:
    if os.environ.get(STORE_ENV):
        return Path(os.environ[STORE_ENV])
    proc = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise LockError(f"{repo} is not a git checkout: {proc.stderr.strip()[:200]}")
    return Path(proc.stdout.strip()) / DIRNAME


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _ago(seconds: float) -> str:
    seconds = int(abs(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def process_started(pid: int) -> str | None:
    """The process's start time as ps prints it: with the pid, it names one process even after pid reuse."""
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return None  # no ps (a minimal container): liveness alone decides
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def holder_gone(record: dict) -> str | None:
    """Why nobody can be holding this lock any more, or None while it may still be held."""
    now = time.time()
    if now >= float(record.get("expires_at", 0)):
        return f"expired {_ago(now - float(record.get('expires_at', 0)))} ago"
    if record.get("host") == socket.gethostname() and record.get("pid"):
        pid = int(record["pid"])
        if not alive(pid):
            return f"its holder (pid {pid}) exited"
        started = process_started(pid)
        if record.get("pid_started") and started and started != record["pid_started"]:
            return f"its holder (pid {pid}) exited and the pid was reused"
    return None


def describe(record: dict) -> str:
    now = time.time()
    return (f"{record.get('surface')} held by {record.get('owner')} ({record.get('op') or '?'}) on "
            f"{record.get('host')} pid {record.get('pid')}, target {str(record.get('sha') or '?')[:12]}, "
            f"since {record.get('acquired')} ({_ago(now - float(record.get('acquired_at', now)))} ago), "
            f"expires in {_ago(float(record.get('expires_at', now)) - now)} unless renewed")


class Store:
    def __init__(self, directory: Path):
        self.dir = directory

    def __enter__(self) -> "Store":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.dir / ".mutex", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        os.close(self.fd)

    def path(self, surface: str) -> Path:
        return self.dir / f"{surface}.json"

    def read(self, surface: str) -> dict | None:
        try:
            record = json.loads(self.path(surface).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"surface": surface, "owner": "unreadable lock file", "expires_at": 0}  # reclaimable, and logged
        return record if isinstance(record, dict) else None

    def write(self, record: dict) -> None:
        path = self.path(record["surface"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1) + "\n")
        tmp.replace(path)

    def remove(self, surface: str) -> None:
        self.path(surface).unlink(missing_ok=True)

    def log(self, event: str, record: dict, **extra) -> None:
        line = {"schema": SCHEMA, "at": _stamp(time.time()), "event": event,
                **{k: record.get(k) for k in ("surface", "owner", "session", "op", "sha", "host", "pid")}, **extra}
        with open(self.dir / "events.jsonl", "a") as fh:
            fh.write(json.dumps(line, separators=(",", ":")) + "\n")


def owner() -> tuple[str, str | None]:
    session = next((os.environ[n].strip() for n in SESSION_ENV if os.environ.get(n, "").strip()), None)
    who = f"{os.environ.get('USER') or 'unknown'}@{socket.gethostname()}"
    return (f"session {session}" if session else who), session


def acquire(directory: Path, surface: str, sha: str, ttl: int, pid: int, op: str | None) -> int:
    now = time.time()
    who, session = owner()
    with Store(directory) as s:
        current = s.read(surface)
        if current:
            gone = holder_gone(current)
            if gone is None:
                print(f"ring-lock: REFUSED: {describe(current)}.", file=sys.stderr)
                print(f"ring-lock: wait for it (`python3 {Path(__file__).resolve()} status {surface}`) and retry; "
                      "it frees itself when its holder exits or stops renewing it.", file=sys.stderr)
                s.log("refused", {"surface": surface, "owner": who, "session": session, "op": op, "sha": sha,
                                  "host": socket.gethostname(), "pid": pid}, holder=current.get("owner"))
                return 1
            s.log("reclaimed", current, reason=gone, by=who)
            print(f"ring-lock: reclaimed {surface} from {current.get('owner')} ({gone}).", file=sys.stderr)
        record = {"schema": SCHEMA, "surface": surface, "token": secrets.token_hex(16), "op": op, "sha": sha,
                  "owner": who, "session": session, "host": socket.gethostname(), "pid": pid,
                  "pid_started": process_started(pid), "ttl_s": ttl, "acquired": _stamp(now), "acquired_at": now,
                  "renewed_at": now, "expires_at": now + ttl, "expires": _stamp(now + ttl)}
        s.write(record)
        s.log("acquired", record, ttl_s=ttl)
    print(record["token"])
    return 0


def renew(directory: Path, surface: str, token: str, ttl: int, sha: str | None) -> int:
    now = time.time()
    with Store(directory) as s:
        current = s.read(surface)
        if not current or current.get("token") != token:
            holder = describe(current) if current else "nobody holds it"
            print(f"ring-lock: this run no longer holds {surface} ({holder}).", file=sys.stderr)
            return 1
        current.update(renewed_at=now, expires_at=now + ttl, expires=_stamp(now + ttl), ttl_s=ttl)
        if sha:
            current["sha"] = sha
        s.write(current)
    return 0


def release(directory: Path, surface: str, token: str) -> int:
    with Store(directory) as s:
        current = s.read(surface)
        if not current or current.get("token") != token:
            return 0  # reclaimed after it expired, or never taken: nothing of ours to release
        s.remove(surface)
        s.log("released", current, held_s=round(time.time() - float(current.get("acquired_at", time.time()))))
    return 0


def status(directory: Path, surface: str | None, as_json: bool) -> int:
    records = []
    if directory.is_dir():
        with Store(directory) as s:
            names = [surface] if surface else sorted(p.stem for p in directory.glob("*.json"))
            records = [r for r in (s.read(n) for n in names) if r]
    if as_json:
        print(json.dumps([{**{k: v for k, v in r.items() if k != "token"}, "reclaimable": holder_gone(r)}
                          for r in records], indent=1))
        return 0
    for r in records:
        gone = holder_gone(r)
        print(describe(r) + (f" [reclaimable: {gone}]" if gone else ""))
    if not records:
        print(f"ring-lock: {surface or 'no surface'} is free." if surface else "ring-lock: no surface is held.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".", help="any checkout of the clone (default: cwd)")
    sub = parser.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("acquire")
    a.add_argument("surface")
    a.add_argument("--sha", required=True)
    a.add_argument("--ttl", type=int, required=True, help="seconds until it expires unless renewed")
    a.add_argument("--pid", type=int, required=True, help="the holding process (the script, not this helper)")
    a.add_argument("--op", help="what holds it, for the refusal message")
    r = sub.add_parser("renew")
    r.add_argument("surface")
    r.add_argument("--token", required=True)
    r.add_argument("--ttl", type=int, required=True)
    r.add_argument("--sha")
    rel = sub.add_parser("release")
    rel.add_argument("surface")
    rel.add_argument("--token", required=True)
    st = sub.add_parser("status")
    st.add_argument("surface", nargs="?")
    st.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if getattr(args, "surface", None) and not SURFACE_RE.match(args.surface):
            raise LockError(f"bad surface name {args.surface!r} (lowercase letters, digits, . _ -)")
        if getattr(args, "ttl", 1) <= 0:
            raise LockError("--ttl must be positive")
        directory = store(args.repo)
        if args.mode == "acquire":
            return acquire(directory, args.surface, args.sha, args.ttl, args.pid, args.op)
        if args.mode == "renew":
            return renew(directory, args.surface, args.token, args.ttl, args.sha)
        if args.mode == "release":
            return release(directory, args.surface, args.token)
        return status(directory, args.surface, args.json)
    except (LockError, OSError) as exc:
        print(f"ring-lock: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
