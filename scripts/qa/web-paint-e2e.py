#!/usr/bin/env python3
"""Web paint check: a real browser watches a live thinking, tool or subagent turn on a local Runtime Host.

The terminal-truth harness runs the real provider CLI against a scripted fake model and a scratch Runtime Host
(`server/zerg/qa/terminal_truth_kinds.py`). This script adds one more client to that turn: an agent-browser (headless
Chromium) session that opens the session page on the scratch host's own web bundle (`web/dist`) before the prompt goes,
samples the DOM while the turn streams, and asserts what a person would see:

- thinking: one collapsed "Thinking" row, opened and closed again by the `t` key;
- tool: a running row with the real tool name and the output tail so far;
- subagent: a row carrying the subagent label, visible without expanding anything;
- after the turn: no live row left, no duplicated row id and no marker drawn twice.

Screenshots (mid turn, expanded, after turn) and a JSON result per cell go to --out-dir.

    make web-paint ARGS="--cell claude/helm --cell antigravity/helm --out-dir /tmp/agents/web-paint/out"

Needs: web/dist built (`cd web && bunx vite build`), `agent-browser`, the provider CLIs and `longhouse-engine`/`longhouse` that the
terminal-truth harness needs. Not part of CI: it needs the real provider CLIs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from zerg.qa.terminal_truth_kinds import run_kinds_suite  # noqa: E402
from zerg.qa.terminal_truth_verdicts import CellSpec  # noqa: E402

SAMPLER = Path(__file__).with_name("web_paint_sampler.py")
# Cells where the scenario has a live kind. (provider, mode) -> scenarios.
DEFAULT_CELLS = {
    ("claude", "helm"): ("thinking", "tool", "subagent"),
    ("antigravity", "helm"): ("thinking", "tool"),
    ("claude", "console"): ("thinking", "tool", "subagent"),
    ("omp", "helm"): ("thinking", "tool", "subagent"),
}


def _ab(*args: str, timeout: float = 60) -> dict[str, Any]:
    proc = subprocess.run(["agent-browser", *args], capture_output=True, text=True, timeout=timeout, check=False)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}


class BrowserObserver:
    """One browser session per cell. before_submit opens the page and starts the sampler; finish returns its JSON."""

    def __init__(self, spec: CellSpec, out_dir: Path, viewport: tuple[int, int]) -> None:
        self.spec = spec
        self.out_dir = out_dir
        self.viewport = viewport
        self.session = ""
        self.thread: threading.Thread | None = None
        self.stop_file = Path(tempfile.mkdtemp(prefix="web-paint-", dir=out_dir)) / "stop"
        self.result_file = self.stop_file.with_name("result.json")
        self.run_output = ""

    @property
    def label(self) -> str:
        return f"{self.spec.provider}-{self.spec.mode}-{self.spec.scenario}" + ("-phone" if self.viewport[0] < 600 else "")

    def before_submit(self, host_url: str, session_ids: list[str], plan: Any) -> None:
        if not session_ids:
            raise RuntimeError("no session known to the host before the prompt: run Helm cells with warm_session")
        opened = _ab("open", "--identity", "disposable", "--url", "about:blank")
        self.session = opened["result"]["session"]["session_id"]
        config = {
            "url": f"{host_url}/timeline/{session_ids[0]}",
            "scenario": self.spec.scenario,
            "provider": self.spec.provider,
            "mode": self.spec.mode,
            "label": self.label,
            "out_dir": str(self.out_dir),
            "stop_file": str(self.stop_file),
            "result_file": str(self.result_file),
            "no_press": bool(os.environ.get("WEB_PAINT_NO_PRESS")),
            "interval": float(os.environ.get("WEB_PAINT_INTERVAL", "0.2")),
            "width": self.viewport[0],
            "height": self.viewport[1],
            "markers": {g: [s.marker for s in plan.group(g)] for g in ("thinking", "tool_started", "tool_output", "subagent_started", "subagent_text", "reply")},
        }
        script = self.stop_file.with_name("step.py")
        ready = self.stop_file.with_name("ready")
        config["ready_file"] = str(ready)
        script.write_text(f"CONFIG = {config!r}\n" + SAMPLER.read_text())

        def work() -> None:
            proc = subprocess.run(
                ["agent-browser", "run", self.session, "--file", str(script), "--timeout-seconds", "150"],
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            self.run_output = (proc.stdout + proc.stderr)[-4000:]

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not ready.exists():  # the page is loaded and the stream connected
            time.sleep(0.2)
        if not ready.exists():
            raise RuntimeError(f"browser page was not ready: {self.run_output}")

    def turn_over(self) -> None:
        self.stop_file.write_text("stop")

    def finish(self) -> dict[str, Any]:
        try:
            if self.thread:
                self.thread.join(timeout=120)
            if self.result_file.exists():
                return json.loads(self.result_file.read_text())
            return {"ok": False, "failures": ["sampler wrote no result"], "run_output": self.run_output}
        finally:
            if self.session:
                _ab("close", self.session)
            shutil.rmtree(self.stop_file.parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cell", action="append", help="provider/mode, repeatable; default the four web-paint cells")
    parser.add_argument("--scenario", action="append", choices=["thinking", "tool", "subagent"])
    parser.add_argument("--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or ""))
    parser.add_argument("--longhouse-cli", type=Path, default=None)
    parser.add_argument("--viewport", default="1280x900", help="WIDTHxHEIGHT; 390x844 for a phone")
    parser.add_argument("--text-before-tool", action="store_true", help="the tool scenario streams a sentence before the tool call")
    parser.add_argument("--engine-delay-ms", type=int, default=0, help="delay each way between engine and host")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not (ROOT / "web" / "dist" / "index.html").exists():
        parser.error("web/dist is not built: cd web && bunx vite build")
    if not args.engine.is_file():
        parser.error("no longhouse-engine binary: pass --engine")
    width, height = (int(v) for v in args.viewport.split("x"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cells = [tuple(c.split("/")) for c in args.cell] if args.cell else list(DEFAULT_CELLS)
    results: list[dict[str, Any]] = []
    for provider, mode in cells:
        for scenario in args.scenario or DEFAULT_CELLS.get((provider, mode), ("thinking", "tool", "subagent")):
            spec = CellSpec(provider, mode, scenario)
            kind_results = run_kinds_suite(
                [spec],
                engine=args.engine,
                longhouse_cli=args.longhouse_cli,
                warm_session=True,
                text_before_tool=args.text_before_tool,
                engine_delay_ms=args.engine_delay_ms,
                observer_factory=lambda s: BrowserObserver(s, args.out_dir, (width, height)),
                log=lambda m: print(m, file=sys.stderr, flush=True),
            )
            for r in kind_results:
                (args.out_dir / f"{r['cell'].replace('/', '-')}-{scenario}-harness.json").write_text(json.dumps(r, indent=1, default=str))
                seen = r.get("observer") or {"ok": False, "failures": [f"no browser result ({r.get('outcome')}: {r.get('reason')})"]}
                results.append({"cell": r["cell"], "scenario": scenario, "viewport": args.viewport, "harness_outcome": r.get("outcome"), **seen})
    (args.out_dir / "web-paint-results.json").write_text(json.dumps(results, indent=2) + "\n")
    for r in results:
        print(f"{'PASS' if r.get('ok') else 'FAIL'} {r['cell']} {r['scenario']} {r['viewport']}: {'; '.join(r.get('failures') or []) or 'ok'}")
    return 0 if results and all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
