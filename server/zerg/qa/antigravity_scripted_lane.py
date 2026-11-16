"""The credential-free scripted-model lane for Antigravity's Console lifecycle.

The shared Console lifecycle drives a real `agy acp` through the real Machine Agent and Runtime Host. This module
supplies the model: a loopback scripted Gemini server (``scripted_model_server``) answering the lifecycle's own
prompts, and a scratch agy profile that reaches only that endpoint (``scripted_model_profiles``). It exists because
the CI Gemini key is free tier: a live-token cell would fail on quota, not on Longhouse. Nothing here judges the
model; the cell asserts the provider binary, the adapter and the lifecycle (antigravity-parity.md, step 6).

Request matching. agy puts the person's prompt in a user message and appends a system message after it, so the
fixture matches the user text (``match_scope: any_user``) and a conversation's history keeps every earlier prompt in
every later request. Entries are therefore listed newest turn first: a request carries the markers of all earlier
turns, and the first entry that matches must be the current one. A tool result is matched on its own output
(``any_input``, because agy may append its system message after the result), listed before the call that produced it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa import scripted_model_profiles
from zerg.qa import scripted_model_server

# One of the models `agy acp` offers (session/set_model). The scripted endpoint answers whichever is selected, so
# the pin only has to be a model agy accepts; it is the qualification model recorded in the proof.
SCRIPTED_AGY_MODEL = "gemini-4-hydrogen-low"

_PREFIX = "LH_ANTIGRAVITY"
# Turns the lifecycle must have served; a missing one means the cell did not exercise that step.
EXPECTED_TURNS = frozenset(
    {
        "console",
        "resume",
        "steer-first",
        "steer-tool-result",
        "steered",
        "interrupt",
        "post-interrupt",
    }
)

# The Helm lifecycle's turns (antigravity_helm_lifecycle.py): the markers it types and sends, and the tool outputs it
# judges. The Console fixture is script(); this one is helm_script().
HELM_PREFIX = "LH_ANTIGRAVITY_HELM"
# The command prints it through shell arithmetic, so the literal output never appears in the model's own tool call.
HELM_INTERRUPT_TOOL_OUTPUT = "AGY_HELM_INTERRUPT_DONE_8"
HELM_STEER_SLEEP_SECONDS = 15
HELM_INTERRUPT_SLEEP_SECONDS = 8
HELM_EXPECTED_TURNS = frozenset({"launch", "idle", "send-first", "sent", "steer-first", "steered", "interrupt", "recover"})

_steer_tag: str | None = None


def steer_tag() -> str | None:
    """The tag the steer step must use so its tool command can be written into the fixture up front."""

    return _steer_tag


def script(tag: str, search_markers: tuple[dict[str, str], ...] = ()) -> dict[str, Any]:
    """The fixture for the whole lifecycle, newest turn first.

    ``search_markers`` add the transcript-search producer's turns: its prompt asks the model to join three pieces
    into one word, so each marker's joined word is the scripted reply to the prompt that carries its pieces.
    """

    def shell(command: str) -> dict[str, Any]:
        return {"type": "shell", "name": "run_command", "command": command}

    echo = "{{input_marker}}"
    turns: list[dict[str, Any]] = [
        {"id": f"search-{index}", "match": marker["pieces"], "match_scope": "any_user", "reply": marker["marker"]}
        for index, marker in reversed(list(enumerate(search_markers)))
    ]
    turns += [
        {"id": "post-interrupt", "match": f"{_PREFIX}_POST_INTERRUPT_", "match_scope": "any_user", "reply": echo},
        # The interrupted tool normally never returns; if it does, answer like the model would.
        {"id": "interrupt-tool-result", "match": "AGY_INTERRUPT_TOOL_DONE", "match_scope": "any_input", "reply": echo},
        {
            "id": "interrupt",
            "match": f"{_PREFIX}_INTERRUPT_",
            "match_scope": "any_user",
            "tool_call": shell("sleep 8 && echo AGY_INTERRUPT_TOOL_DONE"),
        },
        # A steer that landed: every request after it carries the steer text, tool result or not.
        {"id": "steered", "match": f"{_PREFIX}_STEERED_", "match_scope": "any_user", "reply": echo},
        {"id": "steer-tool-result", "match": f"{_PREFIX}_STEER_TOOL_42_{tag}", "match_scope": "any_input", "reply": echo},
        {
            "id": "steer-first",
            "match": f"{_PREFIX}_STEER_FIRST_",
            "match_scope": "any_user",
            "tool_call": shell(f"sleep 15 && echo {_PREFIX}_STEER_TOOL_$((40+2))_{tag}"),
        },
        {"id": "resume", "match": f"{_PREFIX}_RESUME_", "match_scope": "any_user", "reply": echo},
        {"id": "console", "match": f"{_PREFIX}_CONSOLE_", "match_scope": "any_user", "reply": echo},
    ]
    return {"turns": turns}


def helm_steer_tool_output(tag: str) -> str:
    """What the steer step's slow tool prints; a request that carries it ran after that tool."""

    return f"{HELM_PREFIX}_STEER_TOOL_42_{tag}"


def helm_send_tool_output(tag: str) -> str:
    """What the send step's slow tool prints."""

    return f"{HELM_PREFIX}_SENDTOOL_42_{tag}"


def helm_script(tag: str) -> dict[str, Any]:
    """The fixture for the Helm lifecycle, newest turn first (see the module docstring for why)."""

    def shell(command: str) -> dict[str, Any]:
        return {"type": "shell", "name": "run_command", "command": command}

    echo = "{{input_marker}}"
    turns: list[dict[str, Any]] = [
        {"id": "recover", "match": f"{HELM_PREFIX}_RECOVER_", "match_scope": "any_user", "reply": echo},
        # An interrupted tool's output must never reach the model; if it does the interrupt did not hold, and the
        # oracle reads this turn as served.
        {"id": "interrupt-tool-result", "match": HELM_INTERRUPT_TOOL_OUTPUT, "match_scope": "any_input", "reply": echo},
        {
            "id": "interrupt",
            "match": f"{HELM_PREFIX}_INTERRUPT_",
            "match_scope": "any_user",
            "tool_call": shell(f"sleep {HELM_INTERRUPT_SLEEP_SECONDS} && echo AGY_HELM_INTERRUPT_DONE_$((4+4))"),
        },
        {"id": "steered", "match": f"{HELM_PREFIX}_STEERED_", "match_scope": "any_user", "reply": echo},
        {"id": "steer-tool-result", "match": helm_steer_tool_output(tag), "match_scope": "any_input", "reply": echo},
        {
            "id": "steer-first",
            "match": f"{HELM_PREFIX}_STEER_FIRST_",
            "match_scope": "any_user",
            "tool_call": shell(f"sleep {HELM_STEER_SLEEP_SECONDS} && echo {HELM_PREFIX}_STEER_TOOL_$((40+2))_{tag}"),
        },
        {"id": "sent", "match": f"{HELM_PREFIX}_SENT_", "match_scope": "any_user", "reply": echo},
        {"id": "send-tool-result", "match": helm_send_tool_output(tag), "match_scope": "any_input", "reply": echo},
        {
            "id": "send-first",
            "match": f"{HELM_PREFIX}_SENDTASK_",
            "match_scope": "any_user",
            "tool_call": shell(f"sleep {HELM_STEER_SLEEP_SECONDS} && echo {HELM_PREFIX}_SENDTOOL_$((40+2))_{tag}"),
        },
        # The idle send: a remote message typed into an idle session starts a turn of its own.
        {"id": "idle", "match": f"{HELM_PREFIX}_IDLE_", "match_scope": "any_user", "reply": echo},
        {"id": "launch", "match": f"{HELM_PREFIX}_LAUNCH_", "match_scope": "any_user", "reply": echo},
    ]
    return {"turns": turns}


@dataclass
class ScriptedLane:
    root: Path
    server: scripted_model_server.ThreadedServer
    thread: threading.Thread
    script_path: Path
    environment: dict[str, str]
    expected_turns: frozenset[str] = EXPECTED_TURNS
    steer_tag: str = ""
    dump_dir: Path | None = None

    def close(self) -> None:
        global _steer_tag
        _steer_tag = None
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def receipt(self) -> dict[str, Any]:
        """What the scripted server served, and whether the lifecycle's own turns were among it.

        agy also makes requests that are not lifecycle turns (a title, a summary); those fall through to the server's
        default reply and are counted, not required to match a fixture turn.
        """

        try:
            endpoint, script_sha256, total, requests = scripted_model_profiles.scripted_model_receipt_snapshot(self.environment)
            matched = sorted({str(row["matched_turn_id"]) for row in requests if row.get("matched_turn_id") and row["status"] == "served"})
            failed = [row["request_id"] for row in requests if row["status"] == "failed"]
            missing = sorted(self.expected_turns - set(matched))
            status = "pass" if not missing and not failed else "fail"
            return {
                "status": status,
                "endpoint": endpoint,
                "script_sha256": script_sha256,
                "requests": total,
                "matched_turns": matched,
                "missing_turns": missing,
                "failed_requests": failed,
            }
        except Exception as exc:  # noqa: BLE001 - a missing receipt is evidence about the lane, not a crash
            return {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}


def start(
    root: Path,
    home: Path,
    environment: dict[str, str],
    search_markers: tuple[dict[str, str], ...] = (),
    *,
    helm: bool = False,
) -> ScriptedLane:
    """Serve the fixture on loopback and point a scratch agy profile (``home``) at it.

    ``helm`` serves the Helm lifecycle's fixture and keeps every request body under ``root/model-requests``, which its
    oracle reads to see what the model was sent and when.
    """

    global _steer_tag
    tag = uuid4().hex
    path = root / "scripted-model-fixture.json"
    fixture = helm_script(tag) if helm else script(tag, search_markers)
    path.write_text(json.dumps(fixture, sort_keys=True) + "\n", encoding="utf-8")
    dump_dir = root / "model-requests" if helm else None
    if dump_dir is not None:
        dump_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o600)
    turns, digest = scripted_model_server.load_script(path)
    server = scripted_model_server.ThreadedServer(
        ("127.0.0.1", 0),
        scripted_model_server.MockHandler,
        script_turns=turns,
        script_sha256=digest,
        receipt_file=root / "scripted-model-server-receipt.json",
        dump_dir=dump_dir,
    )
    thread = threading.Thread(target=server.serve_forever, name="antigravity-scripted-model", daemon=True)
    thread.start()
    lane = ScriptedLane(
        root,
        server,
        thread,
        path,
        environment,
        expected_turns=HELM_EXPECTED_TURNS if helm else EXPECTED_TURNS,
        steer_tag=tag,
        dump_dir=dump_dir,
    )
    try:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                with urllib.request.urlopen(f"{server.endpoint}/healthz", timeout=0.5):
                    break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("scripted model server did not become ready on loopback")
                time.sleep(0.01)
        environment[scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV] = server.endpoint
        environment[scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV] = str(path)
        scripted_model_profiles.configure_scripted_model_profile("antigravity", home, environment)
    except BaseException:
        lane.close()
        raise
    _steer_tag = tag
    return lane
