"""Shared oracle and hermetic observations for provider-native controls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_interaction_semantics import INTERACTION_LOCAL_CONTROL_OUTPUT
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import interaction_contract_snapshot

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_BLOCKED = "blocked"
MIN_NEGATIVE_PROOF_QUIESCENCE_SECONDS = 1.5


def _claude_command_content(command: str) -> str:
    command_name, _, args = command.partition(" ")
    return "\n".join(
        (
            f"<local-command-caveat>Caveat: {command_name} is a local command.</local-command-caveat>",
            f"<command-name>{command_name}</command-name>",
            f"<command-message>{command_name.removeprefix('/')}</command-message>",
            f"<command-args>{args}</command-args>",
        )
    )


def _synthetic_raw_event(provider: str, probe: Mapping[str, Any], *, output: bool = False) -> dict[str, Any]:
    command = str((probe.get("input_sequence") or [""])[0])
    kind = str(probe.get("expected_interaction_kind") or "local_control")
    if provider == "claude":
        content = (
            "<local-command-stdout>Set the requested local state.</local-command-stdout>" if output else _claude_command_content(command)
        )
        return {
            "type": "user",
            "message": {"role": "user", "content": content},
            "content_text": content,
            "isMeta": True,
            "interaction_kind": "local_control_output" if output else kind,
            "changes_provider_state": False if output else probe.get("changes_provider_state"),
        }
    markers = probe.get("raw_output_markers") if output else probe.get("raw_markers")
    marker_text = " ".join(str(marker) for marker in markers or ()).strip()
    if kind == "provider_system" and not output:
        return {
            "type": "system",
            "role": "system",
            "content_text": marker_text or command or "provider interaction acknowledgement",
            "interaction_kind": kind,
            "changes_provider_state": probe.get("changes_provider_state"),
            "provider_probe_id": probe.get("probe_id"),
        }
    return {
        "type": "user",
        "role": "user",
        "content_text": marker_text or command or "provider interaction acknowledgement",
        "interaction_kind": INTERACTION_LOCAL_CONTROL_OUTPUT if output else kind,
        "changes_provider_state": False if output else probe.get("changes_provider_state"),
        "provider_probe_id": probe.get("probe_id"),
    }


def generated_fake_observation(provider: str) -> dict[str, Any]:
    """Build a provider-shaped, no-token observation for CI and local tests."""

    probes = interaction_contract_snapshot(provider)
    raw_events: list[dict[str, Any]] = []
    probe_observations: list[dict[str, Any]] = []
    for probe in probes:
        disposition = str(probe.get("disposition") or "")
        if disposition in {"policy_disabled", "upstream_absent"}:
            probe_observations.append(
                {
                    "probe_id": probe["probe_id"],
                    "disposition": disposition,
                    "status": STATUS_NOT_APPLICABLE,
                    "raw_events": [],
                }
            )
            continue
        control = _synthetic_raw_event(provider, probe)
        output = _synthetic_raw_event(provider, probe, output=True)
        probe_events = [control, output]
        if probe.get("expected_model_turn") is True:
            probe_events.append(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "Qualification response."},
                    "provider_probe_id": probe["probe_id"],
                }
            )
        raw_events.extend(probe_events)
        probe_observations.append(
            {
                "probe_id": probe["probe_id"],
                "disposition": disposition,
                "status": "observed",
                "raw_events": probe_events,
            }
        )

    boundary = semantic_boundary_fixture(provider)
    marker = str(boundary["ordinary_marker"])
    ordinary_prompt = dict(boundary["ordinary_event"])
    unknown_slash = dict(boundary["unknown_slash_event"])
    raw_events.extend((ordinary_prompt, unknown_slash))
    return {
        "schema_version": 1,
        "artifact_kind": "provider_interaction_semantics_observation",
        "provider": provider,
        "evidence_class": "hermetic",
        "synthetic": True,
        "probes": probe_observations,
        "raw_events": raw_events,
        "ordinary_marker": marker,
        "unknown_slash_probe": unknown_slash["content_text"],
        "semantic_boundary": boundary,
    }


def semantic_boundary_fixture(provider: str) -> dict[str, Any]:
    """Return the deterministic semantic regression fixture used by live runs.

    Provider-native control probes prove what a harness stored. They cannot
    prove the independent projection boundary for an ordinary message and an
    unknown slash command without fabricating provider rows. Keep that small
    regression fixture explicit and separately labelled instead.
    """

    marker = f"LONGHOUSE_INTERACTION_SEMANTICS_{provider.upper()}_MARKER"
    return {
        "evidence_class": "hermetic",
        "source_kind": "semantic_fixture",
        "provider": provider,
        "ordinary_marker": marker,
        "ordinary_event": {
            "type": "user",
            "role": "user",
            "content_text": f"Reply with exactly {marker}",
            "provider_probe_id": "ordinary_marker_prompt",
        },
        "unknown_slash_event": {
            "type": "user",
            "role": "user",
            "content_text": "/custom-command-that-provider-may-send-to-model",
            "provider_probe_id": "unknown_slash_prompt",
        },
    }


def _event_semantics(
    provider: str,
    event: Mapping[str, Any],
    *,
    source_surface: str = "helm_tui",
    sequence_context: dict[str, Any] | None = None,
    allow_parser_semantics: bool = True,
) -> dict[str, Any]:
    parser_interaction_kind = (
        str(event["interaction_kind"]) if allow_parser_semantics and isinstance(event.get("interaction_kind"), str) else None
    )
    parser_changes_provider_state = (
        event["changes_provider_state"] if allow_parser_semantics and isinstance(event.get("changes_provider_state"), bool) else None
    )
    return classify_provider_interaction(
        provider,
        role=str(event.get("role") or event.get("type") or ""),
        content_text=str(event.get("content_text") or event.get("text") or ""),
        raw_json=event.get("raw_json") or event,
        source_surface=source_surface,
        interaction_kind=parser_interaction_kind,
        changes_provider_state=parser_changes_provider_state,
        sequence_context=sequence_context,
    )


def _event_semantics_sequence(
    provider: str,
    events: list[Mapping[str, Any]],
    *,
    allow_parser_semantics: bool = True,
) -> list[dict[str, Any]]:
    context: dict[str, Any] = {}
    return [
        _event_semantics(
            provider,
            event,
            sequence_context=context,
            allow_parser_semantics=allow_parser_semantics,
        )
        for event in events
    ]


def _event_evidence_text(event: Mapping[str, Any]) -> str:
    """Serialize one raw event for literal marker assertions."""

    return json.dumps(dict(event), ensure_ascii=False, sort_keys=True, default=str)


def raw_event_digest(event: Mapping[str, Any]) -> str:
    """Digest the parsed provider row used by the live provenance gate."""

    encoded = json.dumps(
        dict(event),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _live_raw_provenance(
    row: Mapping[str, Any],
    event_rows: list[Mapping[str, Any]],
    *,
    source_root: str | None,
) -> tuple[str, str | None]:
    """Validate that live semantic rows have a bounded raw-source receipt.

    A terminal transcript and self-reported booleans are not enough to prove a
    provider interaction. The producer must bind each parsed event to a source
    row and provide a stable capture receipt for the complete window.
    """

    source_rows = row.get("native_source_rows")
    if not isinstance(source_rows, list) or len(source_rows) != len(event_rows) or not source_rows:
        return STATUS_BLOCKED, "interaction_raw_provenance_missing"
    if not isinstance(source_root, str) or not source_root.strip():
        return STATUS_BLOCKED, "interaction_raw_provenance_root_missing"
    try:
        resolved_root = Path(source_root).expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
    except OSError:
        return STATUS_FAIL, "interaction_raw_provenance_invalid"
    source_offsets: set[tuple[str, int]] = set()
    source_file_cache: dict[str, tuple[int, str, bytes]] = {}
    for event, source in zip(event_rows, source_rows, strict=True):
        if not isinstance(source, Mapping):
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        source_path = source.get("source_path")
        source_offset = source.get("source_offset")
        source_line = source.get("line")
        line_digest = source.get("line_sha256")
        event_digest = source.get("event_sha256")
        source_binding = source.get("source_binding")
        source_file_bytes = source.get("source_file_bytes")
        source_file_digest = source.get("source_file_sha256")
        if (
            not isinstance(source_path, str)
            or not source_path
            or source_binding != "file_bytes_at_offset"
            or type(source_offset) is not int
            or source_offset < 0
            or not isinstance(source_line, str)
            or not isinstance(line_digest, str)
            or len(line_digest) != 64
            or not isinstance(event_digest, str)
            or event_digest != raw_event_digest(event)
            or type(source_file_bytes) is not int
            or source_file_bytes < 0
            or not isinstance(source_file_digest, str)
            or len(source_file_digest) != 64
        ):
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        if hashlib.sha256(source_line.encode("utf-8")).hexdigest() != line_digest:
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        try:
            parsed_line = json.loads(source_line)
        except (TypeError, json.JSONDecodeError):
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        if not isinstance(parsed_line, Mapping) or raw_event_digest(parsed_line) != event_digest:
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        try:
            candidate_path = Path(source_path).expanduser()
            resolved_path = (resolved_root / candidate_path if not candidate_path.is_absolute() else candidate_path).resolve(strict=True)
            resolved_path.relative_to(resolved_root)
            source_key = str(resolved_path)
            cached_file = source_file_cache.get(source_key)
            if cached_file is None:
                file_bytes = resolved_path.read_bytes()
                cached_file = (len(file_bytes), hashlib.sha256(file_bytes).hexdigest(), file_bytes)
                source_file_cache[source_key] = cached_file
            file_size, file_digest, file_bytes = cached_file
        except OSError:
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        line_bytes = source_line.encode("utf-8")
        if (
            file_size != source_file_bytes
            or file_digest != source_file_digest
            or file_bytes[source_offset : source_offset + len(line_bytes)] != line_bytes
        ):
            return STATUS_FAIL, "interaction_raw_provenance_invalid"
        location = (source_key, source_offset)
        if location in source_offsets:
            return STATUS_FAIL, "interaction_raw_provenance_duplicate"
        source_offsets.add(location)

    receipt = row.get("capture_receipt")
    if not isinstance(receipt, Mapping):
        return STATUS_BLOCKED, "interaction_capture_receipt_missing"
    stable_capture = (
        type(receipt.get("stable_snapshots")) is int
        and receipt.get("stable_snapshots") >= 3
        and type(receipt.get("stable_seconds")) in {int, float}
        and receipt.get("stable_seconds") >= MIN_NEGATIVE_PROOF_QUIESCENCE_SECONDS
    )
    completed_process_capture = (
        receipt.get("completion_signal") == "process_exit"
        and type(receipt.get("completion_status")) is int
        and receipt.get("completion_status") == 0
    )
    if not (stable_capture or completed_process_capture) or receipt.get("raw_event_count") != len(event_rows):
        return STATUS_BLOCKED, "interaction_capture_receipt_incomplete"
    event_digests = "".join(str(source["event_sha256"]) for source in source_rows)
    expected_window_digest = hashlib.sha256(event_digests.encode("ascii")).hexdigest()
    if receipt.get("window_sha256") != expected_window_digest:
        return STATUS_FAIL, "interaction_capture_receipt_mismatch"
    return STATUS_PASS, None


def _negative_store_inventory(
    source_rows: list[Any],
    *,
    resolved_root: Path,
    provider_store_root: str,
) -> tuple[Path, dict[str, tuple[int, str]]] | None:
    """Rebuild the captured store inventory instead of trusting its counts."""

    store_root_value = provider_store_root
    if not isinstance(store_root_value, str) or not store_root_value.strip() or Path(store_root_value).is_absolute():
        return None
    raw_store_root = resolved_root / store_root_value
    if raw_store_root.is_symlink():
        return None
    try:
        store_root = raw_store_root.resolve(strict=True)
        store_root.relative_to(resolved_root)
        if not store_root.is_dir():
            return None
        inventory_paths = sorted(store_root.rglob("*"))
        if any(path.is_symlink() for path in inventory_paths):
            return None
        inventory_files = {str(path.resolve()) for path in inventory_paths if path.is_file()}
    except (OSError, ValueError):
        return None

    observed_files: dict[str, tuple[int, str]] = {}
    for source in source_rows:
        if not isinstance(source, Mapping):
            return None
        source_path = source.get("source_path")
        source_bytes = source.get("bytes")
        source_digest = source.get("sha256")
        if (
            not isinstance(source_path, str)
            or not source_path
            or Path(source_path).is_absolute()
            or type(source_bytes) is not int
            or source_bytes < 0
            or not isinstance(source_digest, str)
            or len(source_digest) != 64
        ):
            return None
        raw_source_path = resolved_root / source_path
        if raw_source_path.is_symlink():
            return None
        try:
            resolved_path = raw_source_path.resolve(strict=True)
            resolved_path.relative_to(store_root)
            file_bytes = resolved_path.read_bytes()
        except (OSError, ValueError):
            return None
        if len(file_bytes) != source_bytes or hashlib.sha256(file_bytes).hexdigest() != source_digest:
            return None
        resolved_key = str(resolved_path)
        if resolved_key in observed_files:
            return None
        observed_files[resolved_key] = (source_bytes, source_digest)

    if set(observed_files) != inventory_files:
        return None
    return store_root, observed_files


def _live_negative_provenance(
    row: Mapping[str, Any],
    *,
    source_root: str | None,
) -> tuple[str, str | None]:
    """Validate a provider-native receipt proving that no event was stored.

    An empty raw-event list is normally incomplete evidence. A provider
    adapter may opt into the absence form only when it supplies a stable,
    hash-addressed native store snapshot and explicitly records the native
    counts it observed. This proves a bounded negative fact (no persisted
    event in that store), not that every provider-internal side effect is
    impossible. The evaluator rebuilds the store inventory and database counts
    from the copied bytes; producer-supplied counts are never authoritative.
    """

    source_rows = row.get("native_source_rows")
    if not isinstance(source_rows, list) or not source_rows:
        return STATUS_BLOCKED, "interaction_negative_provenance_missing"
    if not isinstance(source_root, str) or not source_root.strip():
        return STATUS_BLOCKED, "interaction_raw_provenance_root_missing"
    try:
        resolved_root = Path(source_root).expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            return STATUS_FAIL, "interaction_negative_provenance_invalid"
    except OSError:
        return STATUS_FAIL, "interaction_negative_provenance_invalid"

    receipt = row.get("capture_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("negative_evidence") is not True:
        return STATUS_BLOCKED, "interaction_negative_capture_receipt_missing"
    stable_capture = (
        receipt.get("completion_signal") == "stable_native_store"
        and type(receipt.get("completion_status")) is int
        and receipt.get("completion_status") == 0
        and type(receipt.get("stable_snapshots")) is int
        and receipt.get("stable_snapshots") >= 3
        and type(receipt.get("stable_seconds")) in {int, float}
        and receipt.get("stable_seconds") >= MIN_NEGATIVE_PROOF_QUIESCENCE_SECONDS
        and receipt.get("raw_event_count") == 0
        and receipt.get("native_event_count") == 0
    )
    if not stable_capture:
        return STATUS_BLOCKED, "interaction_negative_capture_incomplete"

    provider_store_root = receipt.get("provider_store_root")
    if not isinstance(provider_store_root, str):
        return STATUS_BLOCKED, "interaction_negative_store_receipt_missing"
    inventory = _negative_store_inventory(
        source_rows,
        resolved_root=resolved_root,
        provider_store_root=provider_store_root,
    )
    if inventory is None:
        return STATUS_FAIL, "interaction_negative_store_inventory_invalid"
    store_root, observed_files = inventory

    provider_database = receipt.get("provider_database")
    if isinstance(provider_database, Mapping):
        database_path = provider_database.get("source_path")
        database_digest = provider_database.get("source_sha256")
        if (
            provider_database.get("store_kind") != "opencode_sqlite"
            or not isinstance(database_path, str)
            or not database_path
            or Path(database_path).is_absolute()
            or not isinstance(database_digest, str)
            or len(database_digest) != 64
        ):
            return STATUS_FAIL, "interaction_negative_database_receipt_invalid"
        try:
            resolved_database = (resolved_root / database_path).resolve(strict=True)
            resolved_database.relative_to(store_root)
        except (OSError, ValueError):
            return STATUS_FAIL, "interaction_negative_database_receipt_invalid"
        observed_database = observed_files.get(str(resolved_database))
        if observed_database is None or observed_database[1] != database_digest:
            return STATUS_FAIL, "interaction_negative_database_receipt_invalid"
        try:
            database_uri = f"file:{resolved_database}?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as connection:
                counts = {
                    "event_count": int(connection.execute("SELECT count(*) FROM event").fetchone()[0]),
                    "session_count": int(connection.execute("SELECT count(*) FROM session").fetchone()[0]),
                    "message_count": int(connection.execute("SELECT count(*) FROM message").fetchone()[0]),
                    "part_count": int(connection.execute("SELECT count(*) FROM part").fetchone()[0]),
                }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return STATUS_FAIL, "interaction_negative_database_receipt_invalid"
        if any(provider_database.get(key) != value for key, value in counts.items()):
            return STATUS_FAIL, "interaction_negative_database_receipt_invalid"
        if counts != {"event_count": 1, "session_count": 1, "message_count": 0, "part_count": 0}:
            return STATUS_FAIL, "interaction_negative_database_assertion_failed"
    else:
        provider_store = receipt.get("provider_store")
        if not isinstance(provider_store, Mapping):
            return STATUS_BLOCKED, "interaction_negative_store_receipt_missing"
        store_kind = provider_store.get("store_kind")
        if not isinstance(store_kind, str) or not store_kind:
            return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
        if type(provider_store.get("file_count")) is not int or provider_store.get("file_count") != len(observed_files):
            return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
        if store_kind == "codex_rollout_jsonl":
            rollout_file_count = sum(
                1 for path in observed_files if "sessions" in Path(path).relative_to(store_root).parts and path.endswith(".jsonl")
            )
            if (
                type(provider_store.get("rollout_file_count")) is not int
                or provider_store.get("rollout_file_count") != rollout_file_count
                or rollout_file_count != 0
            ):
                return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
        elif store_kind == "claude_jsonl":
            target_command = provider_store.get("target_command")
            if not isinstance(target_command, str) or not target_command:
                return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
            matching_event_count = 0
            for path in observed_files:
                if not path.endswith(".jsonl"):
                    continue
                try:
                    text = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
                matching_event_count += text.count(f"<command-name>{target_command}</command-name>")
            if provider_store.get("matching_event_count") != matching_event_count or matching_event_count != 0:
                return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
        else:
            return STATUS_FAIL, "interaction_negative_store_receipt_invalid"
    return STATUS_PASS, None


def _event_role(event: Mapping[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("role"), str):
        return str(message["role"]).strip().lower()
    return str(event.get("role") or event.get("type") or "").strip().lower()


def evaluate_observation(
    provider: str,
    observation: Mapping[str, Any],
    *,
    source_root: str | None = None,
) -> dict[str, Any]:
    """Evaluate raw probe evidence without asking an LLM to classify it."""

    contract = contract_for_provider(provider)
    if contract is None:
        return {"status": STATUS_FAIL, "failure_code": "provider_contract_missing"}
    declared = {probe.probe_id: probe for probe in contract.interaction_probes}
    observed = observation.get("probes")
    if not isinstance(observed, list):
        return {
            "status": STATUS_BLOCKED,
            "failure_code": "interaction_probe_observations_missing",
            "message": "The provider artifact did not contain per-probe raw observations.",
        }

    assertion_rows: list[dict[str, Any]] = []
    observed_by_id: dict[str, Mapping[str, Any]] = {}
    live_evidence = observation.get("evidence_class") in {"live_no_token", "live_token"} and observation.get("synthetic") is not True
    for row in observed:
        if not isinstance(row, Mapping):
            assertion_rows.append({"status": STATUS_FAIL, "failure_code": "interaction_probe_row_invalid"})
            continue
        probe_id = str(row.get("probe_id") or "")
        if probe_id in observed_by_id:
            assertion_rows.append({"probe_id": probe_id, "status": STATUS_FAIL, "failure_code": "interaction_probe_duplicate"})
            continue
        observed_by_id[probe_id] = row
        probe = declared.get(probe_id)
        if probe is None:
            assertion_rows.append({"probe_id": probe_id, "status": STATUS_FAIL, "failure_code": "interaction_probe_not_declared"})
            continue
        if probe.disposition in {"policy_disabled", "upstream_absent"}:
            assertion_rows.append(
                {
                    "probe_id": probe_id,
                    "status": STATUS_NOT_APPLICABLE,
                    "disposition": probe.disposition,
                }
            )
            continue
        events = row.get("raw_events")
        negative_absence = live_evidence and row.get("status") == "observed_absence"
        if negative_absence:
            provenance_status, provenance_failure_code = _live_negative_provenance(
                row,
                source_root=(
                    source_root
                    if source_root is not None
                    else (observation.get("native_source_root") if isinstance(observation, Mapping) else None)
                ),
            )
            if provenance_status != STATUS_PASS:
                assertion_rows.append(
                    {
                        "probe_id": probe_id,
                        "status": provenance_status,
                        "disposition": probe.disposition,
                        "failure_code": provenance_failure_code,
                    }
                )
                continue
            absence_assertions = {
                "native_event_absent": events == [],
                "terminal_acknowledged": row.get("terminal_acknowledged") is True,
                "control_not_title_eligible": probe.expected_title_eligibility is False,
                "control_not_user_message": probe.expected_title_eligibility is False,
                "expected_model_turn": probe.expected_model_turn is False
                and row.get("capture_complete") is True
                and row.get("post_interaction_quiescent") is True,
                "expected_state_change": probe.changes_provider_state is False and row.get("provider_state_after") is False,
                "raw_markers_present": not probe.raw_markers,
                "raw_output_markers_present": not probe.raw_output_markers,
            }
            status = STATUS_PASS if all(absence_assertions.values()) else STATUS_FAIL
            assertion_rows.append(
                {
                    "probe_id": probe_id,
                    "status": status,
                    "disposition": probe.disposition,
                    "assertions": absence_assertions,
                    "semantic_events": [],
                    "evidence_basis": {
                        "native_record_absence": provenance_status,
                        "terminal_acknowledgement": "provider_oracle",
                        "raw_output_markers": "none_expected",
                    },
                    **({"failure_code": "interaction_negative_assertion_failed"} if status != STATUS_PASS else {}),
                }
            )
            continue
        if not isinstance(events, list) or not events:
            assertion_rows.append(
                {
                    "probe_id": probe_id,
                    "status": STATUS_BLOCKED,
                    "failure_code": str(row.get("failure_code") or "interaction_raw_evidence_missing"),
                }
            )
            continue
        event_rows = [event for event in events if isinstance(event, Mapping)]
        if len(event_rows) != len(events):
            assertion_rows.append(
                {
                    "probe_id": probe_id,
                    "status": STATUS_FAIL,
                    "failure_code": "interaction_probe_row_invalid",
                }
            )
            continue
        semantic_rows = _event_semantics_sequence(
            provider,
            event_rows,
            allow_parser_semantics=not live_evidence,
        )
        first = semantic_rows[0] if semantic_rows else {}
        expected_kind_present = first.get("interaction_kind") == probe.expected_interaction_kind
        evidence_text = "\n".join(_event_evidence_text(event) for event in event_rows)
        output_text = "\n".join(_event_evidence_text(event) for event in event_rows)
        raw_output_markers_present = all(marker in output_text for marker in probe.raw_output_markers)
        unresolved_evidence: list[str] = []
        provenance_status = STATUS_PASS
        provenance_failure_code: str | None = None
        if live_evidence:
            if row.get("status") != "observed":
                unresolved_evidence.append("provider_probe_not_observed")
            provenance_status, provenance_failure_code = _live_raw_provenance(
                row,
                event_rows,
                source_root=(
                    source_root
                    if source_root is not None
                    else (observation.get("native_source_root") if isinstance(observation, Mapping) else None)
                ),
            )
            if provenance_status == STATUS_FAIL:
                assertion_rows.append(
                    {
                        "probe_id": probe_id,
                        "status": STATUS_FAIL,
                        "disposition": probe.disposition,
                        "failure_code": provenance_failure_code,
                    }
                )
                continue
            if provenance_status != STATUS_PASS:
                unresolved_evidence.append(provenance_failure_code or "interaction_raw_provenance_missing")
            assistant_observed = any(_event_role(event) == "assistant" for event in event_rows)
            if probe.expected_model_turn is None:
                # ``None`` means the contract leaves the turn shape open, but
                # the native window still has to prove that no assistant-role
                # transcript row was smuggled into a provider-system probe.
                expected_model_turn = not assistant_observed
            elif row.get("capture_complete") is True and row.get("post_interaction_quiescent") is True:
                # A bounded, quiescent raw window is the evidence for a
                # negative assertion. The classifier cannot manufacture this
                # fact from a control row.
                expected_model_turn = assistant_observed is (probe.expected_model_turn is True)
            else:
                expected_model_turn = None
                unresolved_evidence.append("model_turn_capture_incomplete")

            if probe.changes_provider_state is None:
                expected_state_change = True
            elif probe.changes_provider_state is True:
                # A provider-native stdout/ack marker is positive post-state
                # evidence. This deliberately does not use the classifier's
                # default ``changes_provider_state`` field.
                expected_state_change = raw_output_markers_present
            else:
                if isinstance(row.get("provider_state_after"), bool):
                    expected_state_change = row["provider_state_after"] is False
                else:
                    expected_state_change = None
                    unresolved_evidence.append("provider_state_after_missing")
        else:
            if probe.expected_model_turn is True:
                expected_model_turn = any(
                    _event_role(event) == "assistant" or semantics.get("starts_model_turn") is True
                    for event, semantics in zip(event_rows, semantic_rows, strict=True)
                )
            else:
                expected_model_turn = first.get("starts_model_turn") is probe.expected_model_turn
            expected_state_change = (
                first.get("changes_provider_state") is probe.changes_provider_state if probe.changes_provider_state is not None else True
            )
        assertions = {
            "expected_kind": expected_kind_present,
            "control_not_title_eligible": first.get("title_eligible") is probe.expected_title_eligibility,
            "control_not_user_message": first.get("counts_as_user_message") is False,
            "expected_model_turn": expected_model_turn,
            "expected_state_change": expected_state_change,
            "raw_markers_present": all(marker in evidence_text for marker in probe.raw_markers),
            "raw_output_markers_present": raw_output_markers_present,
        }
        status = STATUS_BLOCKED if unresolved_evidence else STATUS_PASS if all(assertions.values()) else STATUS_FAIL
        failure_code = None
        if unresolved_evidence:
            failure_code = "interaction_post_state_evidence_missing"
        assertion_rows.append(
            {
                "probe_id": probe_id,
                "status": status,
                "disposition": probe.disposition,
                "assertions": assertions,
                "semantic_events": semantic_rows,
                "evidence_basis": (
                    {
                        "capture_complete": row.get("capture_complete") is True,
                        "post_interaction_quiescent": row.get("post_interaction_quiescent") is True,
                        "raw_provenance": provenance_status,
                        "raw_output_markers": "raw_events",
                    }
                    if live_evidence
                    else {"classifier": "provider_interaction_semantics"}
                ),
                **({"failure_code": failure_code or provenance_failure_code} if failure_code or provenance_failure_code else {}),
            }
        )

    for probe in contract.interaction_probes:
        if probe.probe_id in observed_by_id:
            continue
        if probe.disposition in {"policy_disabled", "upstream_absent"}:
            assertion_rows.append(
                {
                    "probe_id": probe.probe_id,
                    "status": STATUS_NOT_APPLICABLE,
                    "disposition": probe.disposition,
                }
            )
        else:
            assertion_rows.append(
                {
                    "probe_id": probe.probe_id,
                    "status": STATUS_BLOCKED,
                    "disposition": probe.disposition,
                    "failure_code": "interaction_probe_not_observed",
                }
            )

    raw_events = observation.get("raw_events")
    raw_events = raw_events if isinstance(raw_events, list) else []
    all_probes_inapplicable = all(probe.disposition in {"policy_disabled", "upstream_absent"} for probe in contract.interaction_probes)
    if not (all_probes_inapplicable and not raw_events):
        if live_evidence:
            boundary = observation.get("semantic_boundary")
            if (
                not isinstance(boundary, Mapping)
                or boundary.get("evidence_class") != "hermetic"
                or boundary.get("source_kind") != "semantic_fixture"
            ):
                assertion_rows.append(
                    {
                        "probe_id": "shared_title_boundary",
                        "status": STATUS_BLOCKED,
                        "failure_code": "interaction_title_boundary_missing",
                        "evidence_basis": "hermetic_semantic_regression_required",
                    }
                )
            else:
                ordinary_event = boundary.get("ordinary_event")
                unknown_event = boundary.get("unknown_slash_event")
                if not isinstance(ordinary_event, Mapping) or not isinstance(unknown_event, Mapping):
                    assertion_rows.append(
                        {
                            "probe_id": "shared_title_boundary",
                            "status": STATUS_BLOCKED,
                            "failure_code": "interaction_title_boundary_missing",
                            "evidence_basis": "hermetic_semantic_regression_required",
                        }
                    )
                else:
                    marker_semantics = _event_semantics(
                        provider,
                        ordinary_event,
                        allow_parser_semantics=False,
                    )
                    unknown_semantics = _event_semantics(
                        provider,
                        unknown_event,
                        allow_parser_semantics=False,
                    )
                    boundary_assertions = {
                        "ordinary_marker_is_title_eligible": marker_semantics.get("title_eligible") is True,
                        "ordinary_marker_is_user_message": marker_semantics.get("counts_as_user_message") is True,
                        "unknown_slash_remains_eligible": unknown_semantics.get("title_eligible") is True,
                    }
                    boundary_status = STATUS_PASS if all(boundary_assertions.values()) else STATUS_FAIL
                    assertion_rows.append(
                        {
                            "probe_id": "shared_title_boundary",
                            "status": boundary_status,
                            "failure_code": None if boundary_status == STATUS_PASS else "interaction_title_boundary_assertion_failed",
                            "assertions": boundary_assertions,
                            "semantic_events": [marker_semantics, unknown_semantics],
                            "evidence_basis": "hermetic_semantic_regression",
                        }
                    )
        else:
            marker = str(observation.get("ordinary_marker") or "")
            marker_event = next(
                (
                    event
                    for event in raw_events
                    if isinstance(event, Mapping) and marker and marker in str(event.get("content_text") or event.get("text") or "")
                ),
                None,
            )
            marker_semantics = (
                _event_semantics(provider, marker_event, allow_parser_semantics=True) if isinstance(marker_event, Mapping) else {}
            )
            unknown_slash = str(observation.get("unknown_slash_probe") or "")
            unknown_event = next(
                (
                    event
                    for event in raw_events
                    if isinstance(event, Mapping)
                    and unknown_slash
                    and unknown_slash == str(event.get("content_text") or event.get("text") or "")
                ),
                None,
            )
            unknown_semantics = (
                _event_semantics(provider, unknown_event, allow_parser_semantics=True) if isinstance(unknown_event, Mapping) else {}
            )
            if marker_semantics and unknown_semantics:
                boundary_assertions = {
                    "ordinary_marker_is_title_eligible": marker_semantics.get("title_eligible") is True,
                    "ordinary_marker_is_user_message": marker_semantics.get("counts_as_user_message") is True,
                    "unknown_slash_remains_eligible": unknown_semantics.get("title_eligible") is True,
                }
                assertion_rows.append(
                    {
                        "probe_id": "shared_title_boundary",
                        "status": STATUS_PASS if all(boundary_assertions.values()) else STATUS_FAIL,
                        "failure_code": None if all(boundary_assertions.values()) else "interaction_title_boundary_assertion_failed",
                        "assertions": boundary_assertions,
                        "semantic_events": [marker_semantics, unknown_semantics],
                    }
                )
            else:
                assertion_rows.append(
                    {
                        "probe_id": "shared_title_boundary",
                        "status": STATUS_BLOCKED,
                        "failure_code": "interaction_title_boundary_observation_missing",
                    }
                )
    statuses = [str(row.get("status") or STATUS_FAIL) for row in assertion_rows]
    if any(status == STATUS_FAIL for status in statuses):
        status = STATUS_FAIL
    elif any(status == STATUS_BLOCKED for status in statuses):
        status = STATUS_BLOCKED
    elif all(status == STATUS_NOT_APPLICABLE for status in statuses):
        status = STATUS_NOT_APPLICABLE
    else:
        status = STATUS_PASS
    sequence_rows = [event for event in raw_events if isinstance(event, Mapping)]
    sequence_semantics = _event_semantics_sequence(
        provider,
        sequence_rows,
        allow_parser_semantics=not live_evidence,
    )
    provider_status = status if live_evidence else STATUS_NOT_APPLICABLE
    return {
        "status": status,
        "semantic_engine_status": status,
        "provider_status": provider_status,
        "verification_scope": "provider_native" if live_evidence else "semantic_engine",
        "provider": provider,
        "probe_count": len(declared),
        "assertions": assertion_rows,
        "raw_event_count": len(raw_events),
        "semantic_projection": [
            {"event": event, "semantics": semantics} for event, semantics in zip(sequence_rows, sequence_semantics, strict=True)
        ],
        "failure_code": None if status in {STATUS_PASS, STATUS_NOT_APPLICABLE} else "interaction_semantics_assertion_failed",
    }


def jsonl_events(observation: Mapping[str, Any]) -> str:
    rows = observation.get("raw_events")
    if not isinstance(rows, list):
        return ""
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows if isinstance(row, Mapping))


__all__ = ["evaluate_observation", "generated_fake_observation", "jsonl_events", "semantic_boundary_fixture"]
