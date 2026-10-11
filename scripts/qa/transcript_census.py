#!/usr/bin/env python3
"""Census of provider transcript shapes, checked against the shape catalog.

Providers publish no list of what their terminal renders. The only way to
notice a new, changed or vanished transcript line is to fingerprint what they
write and compare it with what we have catalogued. A shape is the provider
plus the entrypoint plus the full discriminator path (`system/turn_duration`,
`attachment/hook_success`, `event_msg/item_completed/CommandExecution`),
never just the top-level type, and each shape carries the set of keys the
provider has ever written on it, so a renamed or dropped field under a known
discriminator is a finding, not a silently ignored line.

    transcript_census.py census  --provider claude PATH [PATH ...]
        Fingerprint transcripts (files or directories) and print the census.
    transcript_census.py check   --provider claude PATH [PATH ...]
        Exit 1 if any observed shape is not classified in the catalog, if a
        shape lacks a key the parser requires (`required_keys`), or if a
        shape carries a key the catalog has never seen.
        A row may also require `with:<shape>`: another shape in the same
        transcript file, for a fact the parser makes from two lines. It is
        counted per file, not per turn.
    transcript_census.py seed    --provider claude PATH [PATH ...]
        Add every unseen shape to the catalog as `unclassified`, widen each
        shape's key set and version range. Rows and keys are never removed;
        `required_keys` is maintained by hand from what the parser reads.

The catalog lives in `schemas/transcript_shapes/<provider>.json` and is
append-only: `first_seen_version` and `last_seen_version` make it the
longitudinal record of which harness added which display feature when.

A parser that dispatches on each content block handles every combination of
the blocks it has an arm for, and a row per combination cannot keep up with
that: four block types are fifteen shapes. So a catalog may also declare
`families`. A family gives one entry per segment of the shape path, either a
literal or the names that segment may hold, and a shape belongs to it when
every `+`-joined name in every segment is listed. Its row is the union of
what those names carry (`keys`, `required_keys`), merged with the shape's own
row when it has one. A name with no `keys` has never been observed, so a
shape that uses it has no baseline to compare new keys against. One entry
per parser arm therefore classifies every shape the arms can produce, and a
block type the parser has no arm for still makes its shape unclassified.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "schemas" / "transcript_shapes"
SUPPORTED_PROVIDERS = (
    "claude",
    "codex",
    "cursor",
    "antigravity",
    "opencode",
    "pi",
    "omp",
)
_CURSOR_CONTEXT_MARKERS = (
    "<user_info>",
    "<agent_transcripts>",
    "<rules>",
    "<system_reminder>",
    "<attached_files>",
    "<system_notification>",
)


def _sorted_keys(obj: object) -> list[str]:
    return sorted(obj.keys()) if isinstance(obj, dict) else []


def _nested_keys(obj: object, prefix: str) -> list[str]:
    """`prefix.key` for each key of a nested object the parser reads into."""
    return [f"{prefix}.{key}" for key in _sorted_keys(obj)]


def _version_key(value: str) -> tuple:
    """Order CLI versions, a prerelease (`0.163.0-alpha.5`) before its release.

    Numeric and non-numeric parts are tagged so two versions always compare:
    providers ship prerelease tags, and a census can see both kinds at once.
    """

    def parts(text: str) -> tuple:
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part) for part in text.split(".")
        )

    core, _, prerelease = value.partition("-")
    return parts(core), 0 if prerelease else 1, parts(prerelease) if prerelease else ()


def claude_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """(shape, keys, version, entrypoint) for one Claude Code JSONL line."""
    kind = str(entry.get("type", "?"))
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    entrypoint = (
        entry.get("entrypoint") if isinstance(entry.get("entrypoint"), str) else None
    )
    if kind == "system":
        keys = _sorted_keys(entry) + _nested_keys(
            entry.get("compactMetadata"), "compactMetadata"
        )
        return f"system/{entry.get('subtype', '?')}", keys, version, entrypoint
    if kind == "attachment":
        att = entry.get("attachment") or {}
        return (
            f"attachment/{att.get('type', '?')}",
            _sorted_keys(att),
            version,
            entrypoint,
        )
    if kind in ("assistant", "user"):
        msg = entry.get("message") or {}
        content = msg.get("content")
        block_types: set[str] = set()
        if isinstance(content, list):
            block_types.update(
                str(block.get("type", "?"))
                for block in content
                if isinstance(block, dict)
            )
        elif isinstance(content, str):
            block_types.add("string")
        if kind == "user" and entry.get("isCompactSummary"):
            block_types.add("compact_summary")
        if kind == "user" and entry.get("isMeta"):
            block_types.add("meta")
        origin = entry.get("origin")
        if (
            kind == "user"
            and isinstance(origin, dict)
            and isinstance(origin.get("kind"), str)
        ):
            block_types.add(f"origin_{origin['kind'].replace('-', '_')}")
        keys = _sorted_keys(entry)
        if isinstance(origin, dict):
            keys += _nested_keys(origin, "origin")
        if kind == "assistant":
            keys = keys + ["usage." + key for key in _sorted_keys(msg.get("usage"))]
        return (
            f"{kind}/" + "+".join(sorted(block_types) or ["?"]),
            keys,
            version,
            entrypoint,
        )
    return kind, _sorted_keys(entry), version, entrypoint


def _full_envelope(text: str, opening: str, closing: str) -> bool:
    value = text.strip()
    return (
        value.startswith(opening)
        and value.endswith(closing)
        and bool(value[len(opening) : -len(closing)].strip())
    )


def codex_provider_system_text(text: str) -> bool:
    value = text.strip()
    if value.startswith("<codex_internal_context"):
        suffix = value[len("<codex_internal_context") :]
        if suffix and suffix[0] in " \t\r\n>":
            opening_end = value.find(">")
            if opening_end >= 0 and _full_envelope(
                value, value[: opening_end + 1], "</codex_internal_context>"
            ):
                return True
    if (
        value.startswith("# AGENTS.md instructions")
        and "<INSTRUCTIONS>" in value
        and value.endswith("</INSTRUCTIONS>")
    ):
        return True
    return any(
        _full_envelope(value, opening, closing)
        for opening, closing in (
            ("<environment_context>", "</environment_context>"),
            ("<permissions instructions>", "</permissions instructions>"),
            ("<collaboration_mode>", "</collaboration_mode>"),
            ("<turn_aborted>", "</turn_aborted>"),
        )
    )


def codex_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """(shape, keys, version, entrypoint) for one Codex rollout line."""
    kind = str(entry.get("type", "?"))
    payload = entry.get("payload") or {}
    version = None
    entrypoint = None
    if kind == "session_meta" and isinstance(payload, dict):
        version = (
            payload.get("cli_version")
            if isinstance(payload.get("cli_version"), str)
            else None
        )
        entrypoint = (
            payload.get("originator")
            if isinstance(payload.get("originator"), str)
            else None
        )
    if kind == "event_msg" and isinstance(payload, dict):
        ptype = str(payload.get("type", "?"))
        shape = f"event_msg/{ptype}"
        keys = _sorted_keys(payload)
        if ptype == "item_completed":
            item = payload.get("item") or {}
            shape += "/" + str(item.get("type", "?"))
            keys = _sorted_keys(item)
        elif ptype == "token_count":
            info = payload.get("info") or {}
            keys += _nested_keys(info, "info") + _nested_keys(
                info.get("last_token_usage") if isinstance(info, dict) else None,
                "info.last_token_usage",
            )
        return shape, keys, version, entrypoint
    if kind == "response_item" and isinstance(payload, dict):
        role = payload.get("role")
        shape = f"response_item/{payload.get('type', '?')}" + (
            f"/{role}" if role else ""
        )
        keys = _sorted_keys(payload)
        if role == "user" and payload.get("type") == "message":
            content = payload.get("content")
            if isinstance(content, list) and any(
                isinstance(item, dict)
                and item.get("type") == "input_text"
                and isinstance(item.get("text"), str)
                and codex_provider_system_text(item["text"])
                for item in content
            ):
                shape += "/provider_system"
                keys += [
                    f"content[].{key}"
                    for item in content
                    if isinstance(item, dict)
                    for key in _sorted_keys(item)
                ]
        return shape, keys, version, entrypoint
    if kind == "turn_context" and isinstance(payload, dict):
        settings = (
            (payload.get("collaboration_mode") or {}).get("settings")
            if isinstance(payload.get("collaboration_mode"), dict)
            else None
        )
        return (
            kind,
            _sorted_keys(payload)
            + _nested_keys(settings, "collaboration_mode.settings"),
            version,
            entrypoint,
        )
    return kind, _sorted_keys(payload) or _sorted_keys(entry), version, entrypoint


def _cursor_store_shape(
    entry: dict, kind: str
) -> tuple[str, list[str], str | None, str | None]:
    """A row of Cursor's `store.db`: the root metadata, the root blob, or a message."""
    if kind == "meta":
        keys = [key for key in _sorted_keys(entry) if not key.startswith("_census_")]
        return f"store/meta/{entry.get('_census_meta_key', '?')}", keys, None, None
    if kind == "root":
        return "store/root", list(entry.get("_census_fields") or []), None, None
    content = entry.get("content")
    block_types: set[str] = set()
    keys = [key for key in _sorted_keys(entry) if not key.startswith("_census_")]
    if isinstance(content, list):
        # `?` is a message with no blocks, which the readers render as nothing.
        # A block with no type is something else: neither reader has an arm
        # for it.
        block_types.update(
            str(block.get("type", "untyped"))
            for block in content
            if isinstance(block, dict)
        )
        keys += [
            f"content[].{key}"
            for block in content
            if isinstance(block, dict)
            for key in _sorted_keys(block)
        ]
    elif isinstance(content, str):
        block_types.add("string")
    return (
        f"store/{entry.get('role') or '?'}/" + "+".join(sorted(block_types) or ["?"]),
        keys,
        None,
        None,
    )


def cursor_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """Name a Cursor line by the precedence the engine parses it by.

    A store row is named by `_cursor_store_shape`. A projection line that
    carries a `role` is a Cursor message whatever else it carries, `type`
    included (`extract_events` hands it to `extract_cursor_events`). Without a
    `role` the engine goes by `type`: a line with a `message` is parsed as a
    message of that type, through the path Claude's lines take and not
    Cursor's, so it is named apart (`type/assistant/text`); any other line is
    a lifecycle record such as `turn_ended` (`event/turn_ended`).
    """
    store_kind = entry.get("_census_store")
    if isinstance(store_kind, str):
        return _cursor_store_shape(entry, store_kind)
    message = entry.get("message")
    typed = None
    if not isinstance(entry.get("role"), str) and isinstance(entry.get("type"), str):
        keys = [key for key in _sorted_keys(entry) if not key.startswith("_census_")]
        if not isinstance(message, dict):
            return f"event/{entry['type']}", keys, None, None
        # As the engine does: a `message` line takes its role from the message.
        typed = (
            str(message.get("role") or "?")
            if entry["type"] == "message"
            else entry["type"]
        )
    role = typed if typed is not None else str(entry.get("role") or "?")
    message = message or {}
    content = message.get("content") if isinstance(message, dict) else None
    block_types: set[str] = set()
    if isinstance(content, list):
        block_types.update(
            str(block.get("type", "?")) for block in content if isinstance(block, dict)
        )
    elif isinstance(content, str):
        block_types.add("string")
    if role == "user" and typed is None:
        text_values = []
        if isinstance(content, str):
            text_values.append(content)
        elif isinstance(content, list):
            text_values.extend(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        combined = "\n".join(value for value in text_values if isinstance(value, str))
        if "<user_query>" in combined and "</user_query>" in combined:
            block_types.add("user_query")
        elif any(
            line.lstrip().startswith(marker)
            for line in combined.splitlines()
            for marker in _CURSOR_CONTEXT_MARKERS
        ):
            block_types.add("provider_context")
    keys = [key for key in _sorted_keys(entry) if not key.startswith("_census_")]
    keys += _nested_keys(message, "message")
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    entrypoint = (
        entry.get("entrypoint") if isinstance(entry.get("entrypoint"), str) else None
    )
    return (
        ("type/" if typed is not None else "")
        + f"{role}/"
        + "+".join(sorted(block_types) or ["?"]),
        keys,
        version,
        entrypoint,
    )


def antigravity_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    legacy = entry.get("_census_legacy_document") is True
    kind = str(entry.get("type", "?"))
    source = str(entry.get("source", "legacy" if legacy else "?"))
    keys = [key for key in _sorted_keys(entry) if not key.startswith("_census_")]
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    entrypoint = (
        entry.get("entrypoint") if isinstance(entry.get("entrypoint"), str) else None
    )
    return f"{source}/{kind}", keys, version, entrypoint


def opencode_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    part = entry.get("part") if isinstance(entry.get("part"), dict) else entry
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    part_type = str(part.get("type", entry.get("type", "?")))
    role = str(message.get("role") or entry.get("role") or "?")
    keys = [key for key in _sorted_keys(part) if not key.startswith("_census_")]
    keys += _nested_keys(message, "message")
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    return f"part/{part_type}/{role}", keys, version, None


def pi_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    if entry.get("type") != "message":
        return (
            str(entry.get("type", "?")),
            _sorted_keys(entry),
            entry.get("version") if isinstance(entry.get("version"), str) else None,
            None,
        )
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    role = str(message.get("role") or "?")
    content = message.get("content")
    block_types: set[str] = set()
    if isinstance(content, list):
        block_types.update(
            str(block.get("type", "?")) for block in content if isinstance(block, dict)
        )
    elif isinstance(content, str):
        block_types.add("string")
    keys = [key for key in _sorted_keys(entry) if not key.startswith("_census_")]
    keys += _nested_keys(message, "message")
    if isinstance(content, list):
        keys += [
            f"message.content[].{key}"
            for block in content
            if isinstance(block, dict)
            for key in _sorted_keys(block)
        ]
    usage = message.get("usage")
    if isinstance(usage, dict):
        keys += _nested_keys(usage, "message.usage")
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    return (
        f"message/{role}/" + "+".join(sorted(block_types) or ["?"]),
        keys,
        version,
        None,
    )


def omp_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """OMP writes Pi's session format with more line types; its catalog is its own."""
    return pi_shape(entry)


SHAPERS = {
    "claude": claude_shape,
    "codex": codex_shape,
    "cursor": cursor_shape,
    "antigravity": antigravity_shape,
    "opencode": opencode_shape,
    "pi": pi_shape,
    "omp": omp_shape,
}


def iter_files(paths: list[str], provider: str) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            if provider == "opencode":
                files.extend(sorted(path.rglob("opencode.db")))
                files.extend(sorted(path.rglob("*.jsonl")))
            else:
                files.extend(sorted(path.rglob("*.jsonl")))
                if provider == "antigravity":
                    files.extend(sorted(path.rglob("logs.json")))
                if provider == "cursor":
                    files.extend(sorted(path.rglob("store.db")))
        elif path.is_file():
            files.append(path)
    return files


def _load_open_readonly():
    """The QA producers' one way to open a provider's SQLite store for reading.

    `zerg.qa.provider_sqlite` is loaded by path, not imported: this tool runs
    where the `zerg` package is not installed, beside a checkout that has the
    file. It reads through the write-ahead log, and reads the main file alone
    when a closed store sits in a directory that cannot be written.
    """
    path = REPO_ROOT / "server" / "zerg" / "qa" / "provider_sqlite.py"
    spec = importlib.util.spec_from_file_location("longhouse_provider_sqlite", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.open_readonly


_open_sqlite_readonly = _load_open_readonly()


def _protobuf_fields(data: bytes) -> tuple[list[str], list[bytes]] | None:
    """Field numbers and wire types of one protobuf message, and its field-1 values.

    Cursor's root blob lists the conversation's message blobs as field 1, each
    a 32-byte id, so the length of every field-1 value is part of the shape.
    The values themselves are returned beside the shape, never in it.
    """
    index = 0
    fields: set[str] = set()
    listed: list[bytes] = []

    def varint() -> int:
        nonlocal index
        value = shift = 0
        while True:
            byte = data[index]
            index += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
            if shift > 63:
                raise ValueError("varint is too long")

    try:
        while index < len(data):
            key = varint()
            number, wire = key >> 3, key & 0x07
            if wire == 0:
                varint()
            elif wire == 1:
                index += 8
            elif wire == 2:
                length = varint()
                if number == 1:
                    fields.add(f"field1.len{length}")
                    listed.append(data[index : index + length])
                index += length
            elif wire == 5:
                index += 4
            else:
                return None
            fields.add(f"field{number}.wire{wire}")
    except (IndexError, ValueError):
        return None
    return (sorted(fields), listed) if index == len(data) else None


def _cursor_meta_json(value: object) -> tuple[Any, str]:
    """Decode a `meta` value as the server's Cursor reader does, and say how it was written.

    cursor-agent writes the root metadata as hex-encoded JSON; the reader
    (`zerg.services.cursor_transcript._decode_meta_json`) also accepts plain
    JSON, so the census does too and records which it found.
    """
    raw = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    try:
        return json.loads(bytes.fromhex(raw).decode("utf-8")), "hex"
    except (ValueError, UnicodeDecodeError):
        try:
            return json.loads(raw), "json"
        except ValueError:
            return None, ""


def _cursor_blob_id(value: object) -> bytes | str:
    """A blob id as the reader compares them: by its bytes when it is hex, whatever its case."""
    try:
        return bytes.fromhex(str(value))
    except ValueError:
        return str(value)


def _cursor_store_records(path: Path) -> Iterator[dict[str, Any] | None]:
    """The rows of a Cursor `store.db` that carry a shape, as census records.

    The root metadata row, the root blob it names (protobuf; fields only) and
    every message blob (JSON). That is a superset of what a reader of the
    current conversation opens: messages of abandoned branches stay in the
    store and are fingerprinted too. Other binary blobs are superseded roots
    and tree nodes; they are not records, except one the root lists as a
    message: that is the older chunked format no reader decodes, and it is
    reported as unparseable.

    The store is opened, and the metadata and the root found, the way the
    server's reader does it (`_open_sqlite_readonly`, `_cursor_meta_json`,
    `_cursor_blob_id`), so a store that reader decodes is one the census
    reads. How the metadata was written is part of its shape
    (`encoding:hex`), because not every reader takes both forms.
    """
    try:
        connection = _open_sqlite_readonly(path)
    except sqlite3.Error:
        yield None
        return
    try:
        root_id: bytes | str | None = None
        for key, value in connection.execute(
            "SELECT key, value FROM meta ORDER BY key"
        ):
            decoded, encoding = _cursor_meta_json(value)
            if not isinstance(decoded, dict):
                yield None
                continue
            if str(key) == "0" and decoded.get("latestRootBlobId"):
                root_id = _cursor_blob_id(decoded["latestRootBlobId"])
            yield {
                **decoded,
                f"encoding:{encoding}": True,
                "_census_store": "meta",
                "_census_meta_key": str(key),
            }
        binary: set[bytes | str] = set()
        listed: list[bytes] = []
        for blob_id, data in connection.execute("SELECT id, data FROM blobs"):
            raw = data if isinstance(data, bytes) else str(data).encode()
            try:
                message = json.loads(raw)
            except ValueError:
                message = None
            if isinstance(message, dict):
                yield {**message, "_census_store": "message"}
                continue
            binary.add(_cursor_blob_id(blob_id))
            if root_id is not None and _cursor_blob_id(blob_id) == root_id:
                parsed = _protobuf_fields(raw)
                if parsed is None:
                    yield None
                else:
                    fields, listed = parsed
                    yield {"_census_store": "root", "_census_fields": fields}
        for message_id in listed:
            if message_id in binary:
                yield None
    except sqlite3.Error:
        yield None
    finally:
        connection.close()


def _iter_records(path: Path, provider: str) -> Iterator[dict[str, Any] | None]:
    if provider == "cursor" and path.suffix == ".db":
        yield from _cursor_store_records(path)
        return

    if provider == "opencode" and path.suffix == ".db":
        try:
            connection = _open_sqlite_readonly(path)
            try:
                rows = connection.execute(
                    "SELECT m.data, p.data FROM part p JOIN message m ON m.id = p.message_id ORDER BY p.time_created, p.id"
                )
                for message_raw, part_raw in rows:
                    try:
                        message = json.loads(message_raw)
                        part = json.loads(part_raw)
                    except (TypeError, ValueError):
                        yield None
                        continue
                    if isinstance(message, dict) and isinstance(part, dict):
                        yield {"message": message, "part": part}
            finally:
                connection.close()
        except sqlite3.Error:
            yield None
        return

    if path.suffix == ".json" and provider == "antigravity":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            yield None
            return
        values = (
            value
            if isinstance(value, list)
            else (value.get("messages") if isinstance(value, dict) else None)
        )
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    item = dict(item)
                    item["_census_legacy_document"] = True
                    yield item
            return
        if isinstance(value, dict):
            yield value
        return

    try:
        with path.open("rb") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    yield None
                    continue
                yield entry if isinstance(entry, dict) else None
    except OSError:
        yield None


WRITTEN_WITH = "with:"


def _written_with(provider: str) -> dict[str, list[str]]:
    """Shapes the catalog says a provider writes in the same transcript as another.

    A parser can depend on a second line: Codex's usage fact is made when a
    turn completes, from the `token_count` event written before it. If the
    provider stops writing that event, no line is left to carry a missing
    key, and a census that looks only at lines present reads clean. So a row
    may require `with:<shape>`: the other shape, in the same file. The census
    counts it on the row's lines like any key, and `drift` reports its
    absence the way it reports a dropped field.

    The guarantee is per transcript file, not per turn: a file in which one
    turn has the other shape and a later turn lacks it is not reported. The
    marker is declared in `required_keys` alone; it is not a key the provider
    writes, so it does not belong in `keys`, and `widened` knows it anyway.
    """
    rows = load_catalog(provider).get("shapes") or {}
    expected = {
        shape: [
            key[len(WRITTEN_WITH) :]
            for key in row.get("required_keys") or []
            if isinstance(key, str) and key.startswith(WRITTEN_WITH)
        ]
        for shape, row in rows.items()
    }
    return {shape: others for shape, others in expected.items() if others}


def cli_versions(observed: dict[str, dict[str, Any]]) -> list[str]:
    """Every CLI version the lines a census read state, oldest first."""
    versions = {
        version
        for record in observed.values()
        for version in record.get("versions") or []
    }
    return sorted(versions, key=_version_key)


def census(files: list[Path], provider: str) -> dict[str, dict[str, Any]]:
    shaper = SHAPERS[provider]
    written_with = _written_with(provider)
    shapes: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "files": set(),
            "versions": set(),
            "entrypoints": set(),
            "key_counts": defaultdict(int),
        }
    )
    for path in files:
        version = None
        entrypoint = None
        in_file: dict[str, int] = defaultdict(int)
        try:
            for entry in _iter_records(path, provider):
                if entry is None:
                    record = shapes["<unparseable>"]
                    record["count"] += 1
                    record["files"].add(str(path))
                    continue
                shape, keys, seen_version, seen_entrypoint = shaper(entry)
                version = seen_version or version
                entrypoint = seen_entrypoint or entrypoint
                record = shapes[shape]
                record["count"] += 1
                in_file[shape] += 1
                record["files"].add(str(path))
                if version:
                    record["versions"].add(version)
                if entrypoint:
                    record["entrypoints"].add(entrypoint)
                # Once per line: a key two blocks of one message both carry
                # must not hide a line that lacks it.
                for key in dict.fromkeys(keys):
                    record["key_counts"][key] += 1
        except OSError:
            pass
        for shape, others in written_with.items():
            for other in others:
                if shape in in_file and other in in_file:
                    shapes[shape]["key_counts"][WRITTEN_WITH + other] += in_file[shape]
    out: dict[str, dict[str, Any]] = {}
    for shape, record in sorted(shapes.items()):
        versions = sorted(record["versions"], key=_version_key)
        out[shape] = {
            "count": record["count"],
            "files": len(record["files"]),
            "first_seen_version": versions[0] if versions else None,
            "last_seen_version": versions[-1] if versions else None,
            "versions": versions,
            "entrypoints": sorted(record["entrypoints"]),
            # Every key the shape carried on any line, and on how many lines:
            # the identity a renamed or dropped field shows up in.
            "keys": sorted(record["key_counts"]),
            "key_counts": dict(sorted(record["key_counts"].items())),
        }
    return out


def catalog_path(provider: str) -> Path:
    return CATALOG_DIR / f"{provider}.json"


def load_catalog(provider: str) -> dict[str, Any]:
    path = catalog_path(provider)
    if not path.exists():
        return {"provider": provider, "shapes": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_catalog(provider: str, catalog: dict[str, Any]) -> None:
    catalog["shapes"] = dict(sorted(catalog["shapes"].items()))
    catalog_path(provider).parent.mkdir(parents=True, exist_ok=True)
    catalog_path(provider).write_text(
        json.dumps(catalog, indent=1, sort_keys=False) + "\n", encoding="utf-8"
    )


def _family_row(catalog: dict[str, Any], shape: str) -> dict[str, Any] | None:
    """The row a family gives a shape, or None when no family holds it.

    `baseline` is False when one of the names the shape uses has no recorded
    keys: `keys` is then only what the others are known to carry, and not
    something to compare the shape's keys against.
    """
    segments = shape.split("/")
    for family in catalog.get("families") or []:
        pattern = family.get("segments") or []
        if len(pattern) != len(segments):
            continue
        parts: list[dict[str, Any]] = [family]
        for allowed, segment in zip(pattern, segments):
            if isinstance(allowed, str):
                if allowed != segment:
                    break
                continue
            names = segment.split("+")
            if not all(name in allowed for name in names):
                break
            parts.extend(allowed[name] for name in names)
        else:
            classifications = dict.fromkeys(
                str(part["classification"])
                for part in parts
                if str(part.get("classification") or "").strip()
            )
            return {
                "classification": " + ".join(classifications),
                "keys": sorted(
                    {key for part in parts for key in part.get("keys") or []}
                ),
                "baseline": all(part.get("keys") is not None for part in parts),
                "required_keys": sorted(
                    {key for part in parts for key in part.get("required_keys") or []}
                ),
            }
    return None


def catalog_row(catalog: dict[str, Any], shape: str) -> dict[str, Any] | None:
    """What the catalog says of a shape: its own row, its family's, or both merged.

    A shape's own row keeps its classification and evidence; the keys and
    required keys its family knows are added to it, so a key the parser's arm
    is known to carry is not new on one combination of blocks. `keys` is None
    when nothing has recorded what the shape carries.
    """
    own = (catalog.get("shapes") or {}).get(shape)
    family = _family_row(catalog, shape)
    if family is None:
        return own
    if own is None:
        return {
            "classification": family["classification"],
            "keys": family["keys"] if family["baseline"] else None,
            "required_keys": family["required_keys"],
        }
    merged = dict(own)
    if own.get("keys") is not None:
        merged["keys"] = sorted(set(own["keys"]) | set(family["keys"]))
    else:
        merged["keys"] = family["keys"] if family["baseline"] else None
    merged["required_keys"] = sorted(
        set(own.get("required_keys") or []) | set(family["required_keys"])
    )
    return merged


def unclassified(
    catalog: dict[str, Any], observed: dict[str, dict[str, Any]]
) -> list[str]:
    def classified(shape: str) -> bool:
        row = catalog_row(catalog, shape)
        classification = str((row or {}).get("classification") or "").strip()
        return bool(classification) and classification != "unclassified"

    return sorted(shape for shape in observed if not classified(shape))


def drift(
    catalog: dict[str, Any], observed: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Catalogued shapes observed without a key the parser requires.

    A signal's parser reads specific keys (`durationMs`, `info.last_token_usage.total_tokens`).
    A provider that renames or drops one keeps the discriminator, so only the
    key set can say the fact will silently stop. Reported per shape with how
    many of its lines lacked each key.
    """
    found: dict[str, dict[str, Any]] = {}
    for shape, record in observed.items():
        row = catalog_row(catalog, shape)
        if row is None:
            continue
        required = [
            key for key in (row.get("required_keys") or []) if isinstance(key, str)
        ]
        if not required:
            continue
        key_counts = record.get("key_counts") or {}
        missing = {
            key: record["count"] - int(key_counts.get(key, 0))
            for key in required
            if int(key_counts.get(key, 0)) < record["count"]
        }
        if missing:
            found[shape] = {"missing": missing, "lines": record["count"]}
    return found


def widened(
    catalog: dict[str, Any], observed: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """Catalogued shapes carrying keys the catalog has never recorded: something new to classify.

    A shape whose keys were never recorded (a row or a family name written
    from the parser's arm, before any transcript carried it) has no baseline,
    so nothing on it is new until `seed` records what it carries.
    """
    found: dict[str, list[str]] = {}
    for shape, record in observed.items():
        row = catalog_row(catalog, shape)
        if row is None or row.get("keys") is None:
            continue
        known = set(row["keys"]) | {
            key
            for key in row.get("required_keys") or []
            if isinstance(key, str) and key.startswith(WRITTEN_WITH)
        }
        new_keys = sorted(set(record.get("keys") or []) - known)
        if new_keys:
            found[shape] = new_keys
    return found


def seed(
    catalog: dict[str, Any], observed: dict[str, dict[str, Any]]
) -> tuple[int, int]:
    """Append unseen shapes; widen key sets and version ranges. Never delete.

    An unseen shape is `unclassified` unless a family already classifies it;
    then its row records only that it was seen, and with which keys.
    """
    rows = catalog.setdefault("shapes", {})
    added = widened_rows = 0
    for shape, record in observed.items():
        row = rows.get(shape)
        if row is None:
            family = _family_row(catalog, shape)
            rows[shape] = {
                "classification": (family or {}).get("classification")
                or "unclassified",
                "first_seen_version": record["first_seen_version"],
                "last_seen_version": record["last_seen_version"],
                "entrypoints": record["entrypoints"],
                "keys": record["keys"],
            }
            added += 1
            continue
        changed = False
        for bound, pick in (("first_seen_version", min), ("last_seen_version", max)):
            seen = record.get(bound)
            if seen and (
                not row.get(bound)
                or pick(row[bound], seen, key=_version_key) != row[bound]
            ):
                row[bound] = seen
                changed = True
        merged = sorted(set(row.get("entrypoints") or []) | set(record["entrypoints"]))
        if merged != (row.get("entrypoints") or []):
            row["entrypoints"] = merged
            changed = True
        merged_keys = sorted(set(row.get("keys") or []) | set(record.get("keys") or []))
        if merged_keys != (row.get("keys") or []):
            row["keys"] = merged_keys
            changed = True
        widened_rows += int(changed)
    return added, widened_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("census", "check", "seed"))
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, required=True)
    parser.add_argument("paths", nargs="+")
    parser.add_argument(
        "--json", action="store_true", help="census: emit JSON instead of a table"
    )
    args = parser.parse_args(argv)

    files = iter_files(args.paths, args.provider)
    if not files:
        print("no transcript files found", file=sys.stderr)
        return 2
    observed = census(files, args.provider)

    if args.command == "census":
        if args.json:
            print(
                json.dumps(
                    {
                        "provider": args.provider,
                        "files": len(files),
                        "shapes": observed,
                    },
                    indent=1,
                )
            )
        else:
            print(f"{args.provider}: {len(files)} files, {len(observed)} shapes")
            for shape, record in sorted(
                observed.items(), key=lambda item: -item[1]["count"]
            ):
                print(
                    f"{record['count']:>8} {record['files']:>5} {record['first_seen_version'] or '-':<9} {record['last_seen_version'] or '-':<9} {','.join(record['entrypoints']) or '-':<14} {shape}"
                )
        return 0

    catalog = load_catalog(args.provider)
    if args.command == "seed":
        added, widened_rows = seed(catalog, observed)
        save_catalog(args.provider, catalog)
        print(
            f"{args.provider}: {added} shapes added, {widened_rows} rows widened, {len(catalog['shapes'])} total"
        )
        return 0

    missing = unclassified(catalog, observed)
    drifted = drift(catalog, observed)
    new_keys = widened(catalog, observed)
    if versions := cli_versions(observed):
        print(f"{args.provider}: written by CLI version(s) {', '.join(versions)}")
    if missing:
        print(
            f"{args.provider}: {len(missing)} unclassified shape(s) in {len(files)} file(s):"
        )
        for shape in missing:
            record = observed[shape]
            print(
                f"  {shape}  count={record['count']} versions={record['first_seen_version']}..{record['last_seen_version']} keys={','.join(record['keys'][:12])}"
            )
    if drifted:
        print(
            f"{args.provider}: {len(drifted)} shape(s) missing a key the parser requires:"
        )
        for shape, report in drifted.items():
            gaps = ", ".join(
                f"{key} absent in {count} of {report['lines']} lines"
                for key, count in report["missing"].items()
            )
            print(f"  {shape}  {gaps}")
    if new_keys:
        print(
            f"{args.provider}: {len(new_keys)} shape(s) carry keys the catalog has never seen (seed to accept):"
        )
        for shape, keys in new_keys.items():
            print(f"  {shape}  +{','.join(keys)}")
    if missing or drifted or new_keys:
        return 1
    print(
        f"{args.provider}: {len(observed)} shapes, all classified, every required key present, no new keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
