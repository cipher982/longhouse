#!/usr/bin/env python3
"""Measure a real Claude-history import through a disposable Runtime Host.

Local invocations own one Runtime Host, one Toxiproxy, one scratch HOME, and one
Machine Agent. Remote invocations use an existing disposable Runtime Host and a
pre-minted device token. Results are JSON only; the scratch tree is removed in
``finally``.

Examples:
  python3 scripts/qa/import_bench.py --name baseline-shaped \
    --image ghcr.io/cipher982/longhouse-runtime:<baseline-sha> --shaped
  python3 scripts/qa/import_bench.py --name candidate-unshaped \
    --image ghcr.io/cipher982/longhouse-runtime:6b66fcddb
  python3 scripts/qa/import_bench.py --name hosted-rehearsal \
    --remote-url https://w22-rehearsal.longhouse.ai --token-file /tmp/token
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client as http_client
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCRATCH_PARENT = Path("/tmp/agents/w1-bench")
RESULTS_PARENT = Path("/tmp/agents/w1-bench-results")
TOXIPROXY_IMAGE = "ghcr.io/shopify/toxiproxy:2.12.0"
DEVICE_ID = "8e447278-31b2-42e2-9518-3713dc9ef6cc"


def command(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout)


def docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return command("docker", *args, timeout=timeout)


def port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, payload: dict[str, Any] | None = None, *, bearer: str | None = None,
         token: str | None = None, timeout: int = 15) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    # Hosted Cloudflare policy admits native Longhouse clients, not urllib's
    # default anonymous user agent. Keep remote benchmark probes on that path.
    request.add_header("User-Agent", "longhouse-engine/0.1")
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    if token:
        request.add_header("X-Agents-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            value = {"raw": raw.decode("utf-8", "replace")}
        return exc.code, value, dict(exc.headers.items())
    except (OSError, http_client.HTTPException) as exc:
        # Connection refused/reset while a container starts is "not ready", not a crash.
        return 0, {"transport_error": f"{type(exc).__name__}: {exc}"}, {}


def wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, _body, _headers = http("GET", f"{base_url}/api/readyz", timeout=3)
        if status == 200:
            return
        time.sleep(0.5)
    raise TimeoutError("Runtime Host did not become ready")


def newest_corpus(source: Path, destination: Path, limit_bytes: int) -> dict[str, Any]:
    candidates = [path for path in source.rglob("*.jsonl") if path.is_file() and not path.is_symlink()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    selected: list[tuple[Path, Path]] = []
    total = 0
    for path in candidates:
        size = path.stat().st_size
        if selected and total + size > limit_bytes:
            continue
        selected.append((path, path.relative_to(source)))
        total += size
        if total >= limit_bytes:
            break
    if not selected:
        raise RuntimeError(f"no Claude JSONL files found under {source}")
    listed = hashlib.sha256()
    for source_file, relative in selected:
        listed.update(f"{relative}\t{source_file.stat().st_size}\n".encode())
        target = destination / ".claude" / "projects" / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
    return {"source": str(source), "file_count": len(selected), "bytes": total, "sorted_file_list_sha256": listed.hexdigest()}


def corpus_sessions(home: Path) -> tuple[set[str], set[str]]:
    """Sessions the import must produce, keyed the way the engine keys them.

    A Claude session is one top-level transcript file and its id is the file
    stem. Counting every ``sessionId`` seen inside the lines also counts the
    parents that subagent and resumed transcripts refer to, which never become
    sessions of their own, so the "all imported" set could never be reached.
    """
    all_ids: set[str] = set()
    recent_ids: set[str] = set()
    cutoff = datetime.now(UTC) - timedelta(days=7)
    for path in (home / ".claude" / "projects").glob("*/*.jsonl"):
        has_message = False
        is_recent = False
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or item.get("type") not in {"user", "assistant"}:
                    continue
                has_message = True
                stamp = item.get("timestamp")
                if isinstance(stamp, str):
                    try:
                        is_recent = is_recent or datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= cutoff
                    except ValueError:
                        pass
        if has_message:
            all_ids.add(path.stem)
            if is_recent:
                recent_ids.add(path.stem)
    return all_ids, recent_ids


def interface_rx_bytes(container: str) -> int:
    output = docker("exec", container, "cat", "/proc/net/dev").stdout
    for line in output.splitlines():
        if line.strip().startswith("eth0:"):
            return int(line.split(":", 1)[1].split()[0])
    raise RuntimeError(f"{container} has no eth0 counter")


def container_rss_bytes(container: str) -> int:
    """Read Docker's current resident-memory estimate without host privileges."""
    value = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", container).stdout.split("/")[0].strip()
    match = re.fullmatch(r"([0-9.]+)\s*(B|KiB|MiB|GiB)", value)
    if match is None:
        raise RuntimeError(f"unrecognized Docker memory value: {value!r}")
    number, unit = match.groups()
    scale = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[unit]
    return int(float(number) * scale)


def engine_backlog(db_path: Path) -> dict[str, int] | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
            if not {"pending_source_envelope", "spool_queue"}.issubset(tables):
                return None
            pending_envelopes = int(connection.execute("select count(*) from pending_source_envelope").fetchone()[0])
            spool_pending = int(connection.execute("select count(*) from spool_queue where status = 'pending'").fetchone()[0])
            spool_dead = int(connection.execute("select count(*) from spool_queue where status = 'dead'").fetchone()[0])
            return {"pending_envelopes": pending_envelopes, "spool_pending": spool_pending, "spool_dead": spool_dead}
    except sqlite3.Error:
        return None


def all_readable(base_url: str, token: str, session_ids: set[str]) -> bool:
    """A user can open a session only when its workspace has transcript events."""
    for session_id in session_ids:
        status, _session, _headers = http("GET", f"{base_url}/api/agents/sessions/{session_id}", token=token)
        if status != 200:
            return False
        status, workspace, _headers = http("GET", f"{base_url}/api/agents/sessions/{session_id}/workspace", token=token)
        items = workspace.get("projection", {}).get("items", []) if status == 200 else []
        if not isinstance(items, list) or not any(
            isinstance(item, dict) and item.get("kind") == "event" and isinstance(item.get("event"), dict)
            for item in items
        ):
            return False
    return True


def visible_sessions(base_url: str, token: str, status_counts: dict[int, int]) -> dict[str, str]:
    """Every imported session, whatever its age.

    The machine session list caps ``days_back`` at 90, so a corpus with older
    history could never read as fully imported through it. The archive manifest
    enumerates up to ten years and pages at 200. Imported unmanaged Claude
    transcripts keep their native UUID as the Longhouse id.
    """
    visible: dict[str, str] = {}
    offset = 0
    while True:
        status, listing, _headers = http(
            "GET",
            f"{base_url}/api/agents/sessions/archive-manifest?days_back=3650&limit=200&offset={offset}&include_test=true&include_automation=true&hide_autonomous=false",
            token=token,
            timeout=30,
        )
        if status in status_counts:
            status_counts[status] += 1
        if status != 200:
            return {}
        sessions = listing.get("sessions", [])
        if not isinstance(sessions, list):
            return {}
        for item in sessions:
            session_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(session_id, str) and session_id:
                visible[session_id] = session_id
        offset += len(sessions)
        if offset >= int(listing.get("total", 0)) or not sessions:
            return visible


def stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="result label, e.g. candidate-shaped")
    parser.add_argument("--image", help="immutable GHCR image reference for a local Runtime Host")
    parser.add_argument("--image-commit", default=None, help="recorded Runtime Host source commit")
    parser.add_argument("--remote-url", help="existing disposable Runtime Host URL; skips local Docker and Toxiproxy")
    parser.add_argument("--token-file", type=Path, help="device token for --remote-url")
    parser.add_argument("--shaped", action="store_true", help="apply 25ms each-way latency and 2500 KB/s upload")
    parser.add_argument("--corpus-root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--corpus-bytes", type=int, default=1_500_000_000)
    parser.add_argument("--timeout-secs", type=int, default=3_600)
    parser.add_argument("--keep-logs", action="store_true", help="copy the engine log to /tmp/agents/w1-bench-results")
    args = parser.parse_args()

    if bool(args.remote_url) != bool(args.token_file):
        raise SystemExit("--remote-url and --token-file must be supplied together")
    if not args.remote_url and not args.image:
        raise SystemExit("--image is required without --remote-url")
    if args.remote_url and args.shaped:
        raise SystemExit("--shaped cannot be used with --remote-url")
    if not args.corpus_root.is_dir():
        raise SystemExit(f"corpus root is not a directory: {args.corpus_root}")
    if not args.remote_url and shutil.which("docker") is None:
        raise SystemExit("missing required executable: docker")
    SCRATCH_PARENT.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = f"w1-{uuid.uuid4().hex[:12]}"
    scratch = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=SCRATCH_PARENT))
    network, runtime, proxy = f"{stamp}-net", f"{stamp}-runtime", f"{stamp}-proxy"
    engine: subprocess.Popen[Any] | None = None
    engine_log = None
    started = time.monotonic()
    result: dict[str, Any] = {"schema": "longhouse.import_bench.v1", "name": args.name, "image": args.image,
                              "image_commit": args.image_commit, "remote_url": args.remote_url,
                              "shaped": args.shaped, "status": "fail"}
    try:
        home = scratch / "home"
        corpus = newest_corpus(args.corpus_root, home, args.corpus_bytes)
        all_ids, recent_ids = corpus_sessions(home)
        if not all_ids:
            raise RuntimeError("corpus contains no Claude session IDs")
        result["corpus"] = {**corpus, "sessions": len(all_ids), "recent_sessions": len(recent_ids)}
        if args.remote_url:
            base_url = args.remote_url.rstrip("/")
            token = args.token_file.read_text(encoding="utf-8").strip()
            if not token.startswith("zdt_"):
                raise RuntimeError("token file does not contain a device token")
        else:
            host_port, proxy_port, admin_port = port(), port(), port()
            password = secrets.token_urlsafe(24)
            docker("network", "create", network)
            docker("run", "-d", "--name", runtime, "--network", network, "--memory", "4g", "-p", f"127.0.0.1:{host_port}:8000",
                   "-e", "AUTH_DISABLED=0", "-e", "SINGLE_TENANT=1", "-e", f"LONGHOUSE_PASSWORD={password}",
                   "-e", "JWT_SECRET=import-bench-jwt-secret", "-e", f"FERNET_SECRET={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}",
                   "-e", "INTERNAL_API_SECRET=import-bench-internal-secret", "-e", "LLM_DISABLED=1",
                   "-e", "DATABASE_URL=sqlite:////data/longhouse.db", args.image)
            base_url = f"http://127.0.0.1:{host_port}"
            wait_for_health(base_url)
            status, login, _headers = http("POST", f"{base_url}/api/auth/password", {"password": password})
            if status != 200 or not isinstance(login.get("access_token"), str):
                raise RuntimeError(f"password login failed: HTTP {status} {login}")
            status, minted, _headers = http("POST", f"{base_url}/api/devices/tokens", {"device_id": DEVICE_ID, "name": stamp}, bearer=login["access_token"])
            token = minted.get("token")
            if status != 201 or not isinstance(token, str) or not token.startswith("zdt_"):
                raise RuntimeError(f"device-token mint failed: HTTP {status} {minted}")
        status, capabilities, _headers = http("GET", f"{base_url}/api/agents/storage/v2/capabilities", token=token)
        if status != 200:
            raise RuntimeError(f"storage-v2 capability negotiation failed: HTTP {status} {capabilities}")
        advertised_encodings = capabilities.get("envelope_content_encodings", [])
        if not isinstance(advertised_encodings, list) or not all(isinstance(value, str) for value in advertised_encodings):
            raise RuntimeError(f"invalid envelope encoding capability: {advertised_encodings!r}")
        result["storage_v2_advertised_encodings"] = advertised_encodings
        result["negotiated_envelope_encoding"] = "zstd" if "zstd" in advertised_encodings else "identity"
        if not args.remote_url:
            docker("run", "-d", "--name", proxy, "--network", network, "-p", f"127.0.0.1:{proxy_port}:8666", "-p", f"127.0.0.1:{admin_port}:8474", TOXIPROXY_IMAGE)
            proxy_api = f"http://127.0.0.1:{admin_port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, _body, _headers = http("GET", f"{proxy_api}/version")
                if status == 200:
                    break
                time.sleep(0.2)
            else:
                raise TimeoutError("Toxiproxy did not become ready")
            status, _body, _headers = http("POST", f"{proxy_api}/proxies", {"name": "runtime", "listen": "0.0.0.0:8666", "upstream": f"{runtime}:8000"})
            if status not in (200, 201):
                raise RuntimeError(f"Toxiproxy proxy create failed: HTTP {status}")
            if args.shaped:
                toxics = (("latency-down", "latency", "downstream", {"latency": 25, "jitter": 0}),
                          ("latency-up", "latency", "upstream", {"latency": 25, "jitter": 0}),
                          ("bandwidth-up", "bandwidth", "upstream", {"rate": 2500}))
                for name, kind, stream, attributes in toxics:
                    status, _body, _headers = http("POST", f"{proxy_api}/proxies/runtime/toxics", {"name": name, "type": kind, "stream": stream, "attributes": attributes})
                    if status not in (200, 201):
                        raise RuntimeError(f"Toxiproxy toxic create failed: HTTP {status}")
        engine_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LONGHOUSE_HOME": str(home / ".longhouse"),
                      "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "RUST_LOG": "info"}
        engine_db = scratch / "engine.db"
        engine_log = (scratch / "engine.log").open("w")
        engine_bin = command(sys.executable, "scripts/build/cargo.py", "artifact", "--profile", "release", "--bin", "longhouse-engine").stdout.strip()
        if not Path(engine_bin).is_file():
            raise RuntimeError("release longhouse-engine is not built; run the documented build first")
        wire_start = interface_rx_bytes(runtime) if not args.remote_url else None
        engine_url = base_url if args.remote_url else f"http://127.0.0.1:{proxy_port}"
        engine = subprocess.Popen([engine_bin, "connect", "--url", engine_url, "--token", token, "--db", str(engine_db),
                                    "--compression", "zstd",
                                    "--machine-name", DEVICE_ID, "--fallback-scan-secs", "1", "--spool-replay-secs", "1"], env=engine_env,
                                  stdout=engine_log, stderr=subprocess.STDOUT, start_new_session=True)
        import_started = time.monotonic()
        first, recent, complete = None, None, None
        counts = {429: 0, 503: 0}
        deadline = import_started + args.timeout_secs
        while time.monotonic() < deadline:
            if engine.poll() is not None:
                raise RuntimeError(f"Machine Agent exited early ({engine.returncode})")
            visible = visible_sessions(base_url, token, counts)
            elapsed = round(time.monotonic() - import_started, 3)
            if first is None and visible:
                first = elapsed
            recent_session_ids = {visible[provider_id] for provider_id in recent_ids if provider_id in visible}
            all_session_ids = {visible[provider_id] for provider_id in all_ids if provider_id in visible}
            if recent is None and recent_ids and len(recent_session_ids) == len(recent_ids) and all_readable(base_url, token, recent_session_ids):
                recent = elapsed
            backlog = engine_backlog(engine_db)
            if complete is None and len(all_session_ids) == len(all_ids) and backlog == {"pending_envelopes": 0, "spool_pending": 0, "spool_dead": 0}:
                complete = elapsed
                break
            time.sleep(1)
        result.update({"status": "ok" if complete is not None else "timeout", "wire_bytes_client_to_server": interface_rx_bytes(runtime) - wire_start if wire_start is not None else None,
                       "wire_bytes_note": "remote Runtime Host cannot expose a client wire counter; engine logs and local health retained" if args.remote_url else None,
                       "time_to_first_timeline_s": first, "time_to_recent_readable_s": recent, "time_to_fully_imported_s": complete,
                       "http_status_counts": {str(key): value for key, value in counts.items()}, "engine_backlog": engine_backlog(engine_db),
                        "server_rss_bytes": container_rss_bytes(runtime) if not args.remote_url else None,
                       "elapsed_s": round(time.monotonic() - started, 3), "import_elapsed_s": round(time.monotonic() - import_started, 3)})
    except Exception as exc:
        result.update({"error": f"{type(exc).__name__}: {exc}", "elapsed_s": round(time.monotonic() - started, 3)})
    finally:
        stop_process(engine)
        if engine_log is not None:
            engine_log.close()
            log_text = (scratch / "engine.log").read_text(errors="replace") if (scratch / "engine.log").exists() else ""
            engine_counts = {"429": len(re.findall(r"(?:HTTP|returned) 429", log_text)), "503": len(re.findall(r"(?:HTTP|returned) 503", log_text))}
            result.setdefault("engine_http_status_counts", engine_counts)
            if args.keep_logs and (scratch / "engine.log").exists():
                RESULTS_PARENT.mkdir(mode=0o700, parents=True, exist_ok=True)
                log_path = RESULTS_PARENT / f"{args.name}.engine.log"
                shutil.copy2(scratch / "engine.log", log_path)
                result["engine_log"] = str(log_path)
            status_path = home / ".longhouse" / "agent" / "engine-status.json"
            if status_path.exists():
                try:
                    result["engine_local_health"] = json.loads(status_path.read_text())
                except json.JSONDecodeError:
                    result["engine_local_health"] = {"error": "invalid engine-status.json"}
        for container in (proxy, runtime):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True, check=False)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, text=True, check=False)
        shutil.rmtree(scratch, ignore_errors=True)
        result["cleanup"] = {"scratch_removed": not scratch.exists()}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
