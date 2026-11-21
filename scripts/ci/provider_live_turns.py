#!/usr/bin/env python3
"""One real-model turn per provider, then the transcript census on what each wrote.

    provider_live_turns.py SCRATCH_DIR [--providers codex,claude,...]

Runs the provider CLIs on PATH headless, each in its own HOME under SCRATCH_DIR, with one tool call forced: the model must run a
shell command whose output it cannot guess (a hash of a fresh nonce) and reply with that output. A turn passes when the CLI exits 0
and its reply holds the hash. Then `transcript_census.py check` runs on that provider's transcript directory. This catches what the
scripted fake model cannot: a provider API change, a changed model reply format, a transcript shape the fake never writes.

Claude uses ANTHROPIC_API_KEY directly. Codex, OpenCode, Pi and OMP go through OpenRouter (OPENROUTER_API_KEY). Models come from the
environment (the repo's CONSOLE_QA_*_MODEL variables); a provider whose key or model is unset is a failure, not a skip.
"""

import argparse
import hashlib
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 300
# Where each CLI keeps its transcripts under HOME: the census input.
TRANSCRIPTS = {
    "claude": ".claude/projects",
    "codex": ".codex/sessions",
    "opencode": ".local/share/opencode",
    "pi": ".pi",
    "omp": ".omp",
}


def openrouter_model(var: str) -> str:
    return os.environ[var].removeprefix("openrouter/")  # the id OpenRouter itself uses


def command(provider: str, prompt: str, home: Path) -> tuple[list[str], dict[str, str]]:
    """The headless argv and the extra environment for one provider."""
    if provider == "claude":
        key, model = os.environ["ANTHROPIC_API_KEY"], os.environ["CONSOLE_QA_ANTHROPIC_MODEL"]
        return ["claude", "-p", prompt, "--model", model, "--allowedTools", "Bash"], {"ANTHROPIC_API_KEY": key}
    key = {"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]}
    if provider == "codex":
        model = openrouter_model("CONSOLE_QA_OPENCODE_MODEL")
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text(
            f'model_provider = "openrouter"\nmodel = "{model}"\n\n[model_providers.openrouter]\nname = "OpenRouter"\n'
            'base_url = "https://openrouter.ai/api/v1"\nenv_key = "OPENROUTER_API_KEY"\nwire_api = "responses"\n'
        )
        # The runner cannot start Codex's bubblewrap sandbox, and the command is the one we asked for.
        return ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", prompt], key
    if provider == "opencode":
        return ["opencode", "run", "--model", "openrouter/" + openrouter_model("CONSOLE_QA_OPENCODE_MODEL"), prompt], key
    if provider == "pi":
        return ["pi", "-p", prompt, "--model", os.environ["CONSOLE_QA_PI_MODEL"]], key
    return ["omp", "-p", prompt, "--model", os.environ["CONSOLE_QA_OMP_MODEL"]], {**key, "OMP_SKIP_SETUP": "1"}


def run_provider(provider: str, scratch: Path) -> str | None:
    """Returns None when the turn and the census passed, else why not."""
    nonce = secrets.token_hex(8)
    expected = hashlib.sha1(nonce.encode()).hexdigest()[:10]
    prompt = (
        f"Use your shell tool to run exactly this command: printf %s {nonce} | sha1sum | cut -c1-10\n"
        "Then reply with only the 10 characters it printed. Do not guess them."
    )
    home = scratch / provider / "home"
    work = scratch / provider / "work"
    work.mkdir(parents=True)
    home.mkdir()
    argv, extra = command(provider, prompt, home)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8", "CI": "1", "TERM": "dumb", **extra}
    try:
        done = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"turn did not finish in {TIMEOUT_S} s"
    (scratch / provider / "stdout.txt").write_text(done.stdout)
    (scratch / provider / "stderr.txt").write_text(done.stderr)
    print(f"--- {provider}: exit {done.returncode}\n{done.stdout[-600:]}\n{done.stderr[-600:]}")
    if done.returncode != 0:
        return f"exit {done.returncode}"
    if expected not in done.stdout:
        return f"the reply does not hold the command's output {expected}: the tool call did not happen or its result was lost"
    check = subprocess.run(
        [sys.executable, str(ROOT / "scripts/qa/transcript_census.py"), "check", "--provider", provider, str(home / TRANSCRIPTS[provider])],
        capture_output=True,
        text=True,
    )
    print(check.stdout + check.stderr)
    return None if check.returncode == 0 else f"transcript census exit {check.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("--providers", default=",".join(TRANSCRIPTS))
    args = parser.parse_args()
    failures = {}
    for provider in args.providers.split(","):
        try:
            why = run_provider(provider, args.scratch)
        except KeyError as exc:
            why = f"{exc} is not set"
        print(f"{provider}: {'ok' if why is None else 'FAILED: ' + why}", flush=True)
        if why is not None:
            failures[provider] = why
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
