"""Provider drivers and the scratch stack they run against: Runtime Host, scripted model, per-provider home setup."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.qa import scripted_model_profiles
from zerg.qa.scripted_model_server import Pacing
from zerg.qa.terminal_truth_pty import _AMBIENT_KEEP  # noqa: F401 - re-exported through terminal_truth
from zerg.qa.terminal_truth_pty import _AMBIENT_PREFIXES  # noqa: F401 - re-exported through terminal_truth
from zerg.qa.terminal_truth_pty import SERVER_ROOT
from zerg.qa.terminal_truth_pty import clean_environ
from zerg.qa.terminal_truth_pty import scrub_ambient_environment


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


class ScratchRuntimeHost:
    """A loopback ``longhouse-server serve`` on its own database and HOME. Auth is off on loopback."""

    def __init__(self, root: Path, extra_environment: dict[str, str] | None = None) -> None:
        self.root = root
        self.extra_environment = dict(extra_environment or {})  # a mutation test shadows ``zerg`` with a patched copy here
        self.process: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.token = ""

    def start(self) -> None:
        home = self.root / "host-home"
        home.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        environment = scripted_model_profiles.credential_free_environment(reject_ambient_credentials=False)
        environment.update(HOME=str(home), LLM_DISABLED="1", SKIP_DEMO_SEED="1", ENVIRONMENT="test:e2e")
        scrub_ambient_environment(environment)
        environment.update(self.extra_environment)
        executable = Path(sys.executable).parent / "longhouse-server"
        log = (self.root / "runtime-host.log").open("wb")
        self.process = subprocess.Popen(
            [str(executable), "serve", "--port", str(port), "--db", f"sqlite:///{self.root / 'runtime-host.db'}"],
            cwd=SERVER_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"scratch Runtime Host exited ({self.process.returncode}); see {self.root / 'runtime-host.log'}")
            with contextlib.suppress(OSError, urllib.error.URLError):
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=2):
                    pass
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("scratch Runtime Host did not answer /api/health")
        minted = _post_json(f"{self.url}/api/devices/tokens", {"device_id": "terminal-truth", "name": "terminal-truth"})
        self.token = str(minted["token"])

    def stop(self) -> None:
        _stop_group(self.process)


def _stop_group(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # SIGKILL is pending but the process is still in the kernel (a process stuck in disk wait cannot be reaped
        # until the disk answers). Carry on: the pass is over, and one unkillable host must not discard the run.
        print(f"terminal truth: process {process.pid} did not exit after SIGKILL; continuing", flush=True)


def pace_json(pace: Pacing) -> dict[str, Any]:
    return {
        "tokens": pace.tokens,
        "rate": pace.rate,
        "initial_delay": pace.initial_delay,
        "marker_every": pace.marker_every,
        "marker_prefix": pace.marker_prefix,
        "reasoning_tokens": pace.reasoning_tokens,
    }


class ScriptedModel:
    """The scripted model server as a child process, holding every turn of a pass (one per cell, matched by its nonce)."""

    def __init__(self, root: Path, turns: Sequence[dict[str, Any]], *, log_all_frames: bool = False) -> None:
        self.root = root
        self.log_all_frames = log_all_frames
        self.script = root / "scripted-model.json"
        self.script.write_text(json.dumps({"turns": list(turns)}))
        self.process: subprocess.Popen[bytes] | None = None
        self.endpoint = ""
        self._log = open(root / "model.log", "wb")  # noqa: SIM115 - held for the child's life, closed in stop()

    def start(self) -> None:
        port_file = self.root / "model.port"
        environment = clean_environ(PYTHONPATH=str(SERVER_ROOT))  # not the caller's shell: no hook URL or token
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "zerg.qa.scripted_model_server",
                "--port-file",
                str(port_file),
                "--script",
                str(self.script),
                "--receipt-file",
                str(self.root / "model-receipt.json"),
                "--stream-log",
                str(self.root / "model-stream.jsonl"),
                *(["--stream-log-all"] if self.log_all_frames else []),
                *(["--dump-requests", os.environ["TT_DUMP_REQUESTS_DIR"]] if os.environ.get("TT_DUMP_REQUESTS_DIR") else []),
            ],
            cwd=SERVER_ROOT,
            env=environment,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not port_file.exists():
            raise RuntimeError("scripted model server did not publish a port")
        self.endpoint = f"http://127.0.0.1:{port_file.read_text().strip()}"

    def stop(self) -> None:
        _stop_group(self.process)
        self._log.close()

    def sent_times(self, markers: Sequence[str]) -> dict[str, float | None]:
        """When the model server wrote the first frame carrying each marker to the provider (monotonic)."""

        seen: dict[str, float | None] = dict.fromkeys(markers)
        log = self.root / "model-stream.jsonl"
        if log.exists():
            for line in log.read_text().splitlines():
                row = json.loads(line)
                for marker in markers:
                    if seen[marker] is None and marker in row["frame"]:
                        seen[marker] = row["t"]
        return seen

    def served_requests(self, turn_ids: Sequence[str] | None = None) -> int:
        """Requests the model served, all of them or only those that matched one of ``turn_ids``."""

        with contextlib.suppress(OSError, ValueError, KeyError):
            receipt = json.loads((self.root / "model-receipt.json").read_text())
            if turn_ids is None:
                return int(receipt["scripted_model"]["served_requests"])
            return sum(1 for r in receipt["requests"] if r["status"] == "served" and r["matched_turn_id"] in turn_ids)
        return 0


@dataclass(frozen=True)
class ProviderDriver:
    provider: str
    binary: str
    ready_text: str  # on screen when the TUI accepts a prompt; never empty, so readiness is always checked
    argv: Callable[[Path, Path], list[str]]  # (binary, workspace) -> argv
    answers: tuple[tuple[str, bytes], ...] = ()  # (screen text, keys): startup prompts the harness answers once

    def __post_init__(self) -> None:
        if not self.ready_text.strip():
            raise ValueError(f"{self.provider}: a driver must name text that proves a ready prompt (an empty string checks nothing)")


# Codex's shell sandbox in Helm. On macOS it is Seatbelt and read-only works. On Linux it is bubblewrap, which an
# unprivileged container cannot start: on the CI runner every command the scripted model asked for failed with
# "bwrap: Failed to make / slave: Permission denied", so the tool result never carried the tool token and the second
# message of the two-message scenario was never served (codex/helm, 4 of 8 markers, run 43296023463, 2026-11-12).
# The only commands here are the `echo` lines of the scripted model, in a scratch home, so Linux runs them unsandboxed.
CODEX_HELM_SANDBOX = "read-only" if sys.platform == "darwin" else "danger-full-access"

PROVIDERS: dict[str, ProviderDriver] = {
    "claude": ProviderDriver(
        "claude",
        "claude",
        'Try "',
        lambda b, w: [str(b)],
        answers=(("I am using this for local development", b"\r"),),  # Helm's development-channel warning
    ),
    "codex": ProviderDriver(
        "codex",
        "codex",
        "Ask Codex",
        lambda b, w: [str(b), "--no-alt-screen", "--ask-for-approval", "never", "--sandbox", CODEX_HELM_SANDBOX, "-C", str(w)],
    ),
    "opencode": ProviderDriver(
        "opencode",
        "opencode",
        "ctrl+p commands",
        lambda b, w: [str(b), "--model", "longhouse-mock/mock-model", str(w), "--pure"],
    ),
    # Pi and OMP draw no prompt word. Their footer names the active model once the TUI has loaded it, which also proves
    # the scripted model was selected.
    "pi": ProviderDriver(
        "pi", "pi", "(longhouse-mock) mock-model", lambda b, w: [str(b), "--provider", "longhouse-mock", "--model", "mock-model"]
    ),
    "omp": ProviderDriver("omp", "omp", "Longhouse mock", lambda b, w: [str(b), "--model", "longhouse-mock/mock-model"]),
    "antigravity": ProviderDriver(
        "antigravity", "agy", "Gemini API key", lambda b, w: [str(b)], answers=(("Yes, I trust this folder", b"\r"),)
    ),
}


def provider_environment(provider: str, endpoint: str, script: Path, home: Path) -> dict[str, str]:
    """A credential-free environment for ``provider`` whose only reachable model is the scripted endpoint at ``endpoint``."""

    profiles = scripted_model_profiles
    saved = {k: os.environ.get(k) for k in (profiles.SCRIPTED_MODEL_BASE_URL_ENV, profiles.SCRIPTED_MODEL_SCRIPT_ENV)}
    os.environ[profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    os.environ[profiles.SCRIPTED_MODEL_SCRIPT_ENV] = str(script)
    try:
        if provider in profiles.SCRIPTED_MODEL_API_KEY_BY_PROVIDER:
            environment = profiles.scripted_model_environment(provider)
        else:
            environment = profiles.credential_free_environment(reject_ambient_credentials=False)
            environment[profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for name in [
        n
        for n in environment
        if n.startswith(("CLAUDECODE", "CLAUDE_CODE", "CODEX_", "OMP_")) and n not in {"CODEX_API_KEY", "CODEX_MODEL"}
    ]:
        environment.pop(name)
    scrub_ambient_environment(environment)
    if provider == "omp":
        environment["OMP_SKIP_SETUP"] = "1"  # a fresh HOME otherwise shows a setup wizard that swallows input
    environment[profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    profiles.configure_scripted_model_profile(provider, home, environment)
    if provider == "opencode":
        # Fail closed: only the scripted provider is enabled, so no built-in provider (OpenRouter and the like) can be chosen.
        config = home / ".config" / "opencode" / "opencode.json"
        value = json.loads(config.read_text())
        value["enabled_providers"] = [profiles.SCRIPTED_MODEL_PROVIDER]
        value["model"] = f"{profiles.SCRIPTED_MODEL_PROVIDER}/{profiles.SCRIPTED_MODEL_NAME}"
        config.write_text(json.dumps(value, indent=2))
    return environment


def _version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, env=clean_environ()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else None


def _prepare_provider(
    provider: str, modes: set[str], *, home: Path, root: Path, cli: Path | None, binary: str, penv: dict[str, str], workspace_root: Path
) -> None:
    """Set one provider up inside the pass's HOME (config, login, hooks) before the Machine Agent starts."""

    from zerg.qa import live_session_toolkit as toolkit

    if provider == "codex":
        from zerg.qa.codex_auth import login_with_api_key

        key = penv.pop("CODEX_API_KEY")
        login_with_api_key(Path(binary), api_key=key, environment=penv, cwd=workspace_root, timeout=30)
    if provider == "claude":
        toolkit.prepare_claude_profile(
            binary=Path(binary), home=home, workspace=workspace_root, environment=penv, recording=root / "claude-onboarding.raw"
        )
        settings = Path(penv["CLAUDE_CONFIG_DIR"]) / "settings.json"
        current = json.loads(settings.read_text()) if settings.exists() else {}
        current["skipDangerousModePermissionPrompt"] = True  # the Bypass Permissions screen Helm's flags trigger
        settings.write_text(json.dumps(current))
        # Console advertises claude.turn_start only once the lifecycle hook is in settings.json (Helm installs it at launch).
        if cli is None:
            raise RuntimeError("the native longhouse CLI is needed to install the Claude lifecycle hook")
        subprocess.run(
            [str(cli), "claude", "configure", "--claude-dir", penv["CLAUDE_CONFIG_DIR"]],
            env=penv,
            check=True,
            capture_output=True,
            timeout=60,
        )
    if provider == "antigravity" and "helm" in modes:
        _install_antigravity_hook(home, penv)
    if provider == "pi":
        agent_dir = home / ".pi" / "agent"  # Console launches `pi` with no --model; pin the fake or Pi tries OpenRouter
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "settings.json").write_text(json.dumps({"defaultProvider": "longhouse-mock", "defaultModel": "mock-model"}))
    if provider == "omp":
        # The state that made the launcher pick a session directory OMP does not write: XDG_DATA_HOME unset and
        # ~/.local/share/omp present. Linux runners have it; the harness HOME must too, or the gate cannot see that defect.
        (home / ".local" / "share" / "omp").mkdir(parents=True, exist_ok=True)


def _install_antigravity_hook(home: Path, environment: dict[str, str]) -> None:
    """Put the Longhouse hook where `longhouse antigravity` insists on finding it, inside the isolated ``home`` only.

    Product finding: no command installs the Antigravity hook without also installing the machine service
    (`longhouse-server onboard` and `longhouse machine repair` both do), and the verifier may not import the
    subject's installer. So the harness writes the same files the installer writes: the shipped hook script (its
    source text is read from the subject's file, not imported, and fails loudly if it moves) and a global
    ``hooks.json`` entry, both under ``home``. The real script matters: on Stop it wakes the transcript shipper, which
    a no-op stub does not, and that changes the measured ship lag by seconds.
    """

    import ast

    from zerg.qa.provider_live_canary import _antigravity_hook_config

    if Path(environment["HOME"]).resolve() != home.resolve():
        raise RuntimeError("refusing to install the Antigravity hook outside the isolated HOME")
    source = (SERVER_ROOT / "zerg" / "services" / "antigravity_hook_inbox.py").read_text()
    template = next(
        (
            ast.literal_eval(node.value)
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_ANTIGRAVITY_HOOK_SCRIPT" for t in node.targets)
        ),
        None,
    )
    if not isinstance(template, str) or "__LONGHOUSE_HOME__" not in template:
        raise RuntimeError("the Antigravity hook script template moved; update _install_antigravity_hook")
    plugin = home / ".longhouse" / "managed-local" / "antigravity" / "plugins" / "longhouse-runtime"
    plugin.mkdir(parents=True)
    script = plugin / "longhouse-antigravity-hook.sh"
    script.write_text(template.replace("__LONGHOUSE_HOME__", shlex.quote(str(home / ".longhouse"))))
    script.chmod(0o755)
    (plugin / "plugin.json").write_text(json.dumps({"name": "longhouse-runtime"}) + "\n")
    hooks = home / ".gemini" / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text(json.dumps({"longhouse-runtime": _antigravity_hook_config(script)}, indent=2) + "\n")
