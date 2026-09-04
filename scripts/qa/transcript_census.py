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
    transcript_census.py seed    --provider claude PATH [PATH ...]
        Add every unseen shape to the catalog as `unclassified`, widen each
        shape's key set and version range. Rows and keys are never removed;
        `required_keys` is maintained by hand from what the parser reads.

The catalog lives in `schemas/transcript_shapes/<provider>.json` and is
append-only: `first_seen_version` and `last_seen_version` make it the
longitudinal record of which harness added which display feature when.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "schemas" / "transcript_shapes"
SUPPORTED_PROVIDERS = ("claude", "codex")


def _sorted_keys(obj: object) -> list[str]:
    return sorted(obj.keys()) if isinstance(obj, dict) else []


def _nested_keys(obj: object, prefix: str) -> list[str]:
    """`prefix.key` for each key of a nested object the parser reads into."""
    return [f"{prefix}.{key}" for key in _sorted_keys(obj)]


def _version_key(value: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in value.split("."))


def claude_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """(shape, keys, version, entrypoint) for one Claude Code JSONL line."""
    kind = str(entry.get("type", "?"))
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    entrypoint = entry.get("entrypoint") if isinstance(entry.get("entrypoint"), str) else None
    if kind == "system":
        keys = _sorted_keys(entry) + _nested_keys(entry.get("compactMetadata"), "compactMetadata")
        return f"system/{entry.get('subtype', '?')}", keys, version, entrypoint
    if kind == "attachment":
        att = entry.get("attachment") or {}
        return f"attachment/{att.get('type', '?')}", _sorted_keys(att), version, entrypoint
    if kind in ("assistant", "user"):
        msg = entry.get("message") or {}
        content = msg.get("content")
        block_types: set[str] = set()
        if isinstance(content, list):
            block_types.update(str(block.get("type", "?")) for block in content if isinstance(block, dict))
        elif isinstance(content, str):
            block_types.add("string")
        if kind == "user" and entry.get("isCompactSummary"):
            block_types.add("compact_summary")
        if kind == "user" and entry.get("isMeta"):
            block_types.add("meta")
        keys = _sorted_keys(entry)
        if kind == "assistant":
            keys = keys + ["usage." + key for key in _sorted_keys(msg.get("usage"))]
        return f"{kind}/" + "+".join(sorted(block_types) or ["?"]), keys, version, entrypoint
    return kind, _sorted_keys(entry), version, entrypoint


def codex_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """(shape, keys, version, entrypoint) for one Codex rollout line."""
    kind = str(entry.get("type", "?"))
    payload = entry.get("payload") or {}
    version = None
    entrypoint = None
    if kind == "session_meta" and isinstance(payload, dict):
        version = payload.get("cli_version") if isinstance(payload.get("cli_version"), str) else None
        entrypoint = payload.get("originator") if isinstance(payload.get("originator"), str) else None
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
                info.get("last_token_usage") if isinstance(info, dict) else None, "info.last_token_usage"
            )
        return shape, keys, version, entrypoint
    if kind == "response_item" and isinstance(payload, dict):
        role = payload.get("role")
        shape = f"response_item/{payload.get('type', '?')}" + (f"/{role}" if role else "")
        return shape, _sorted_keys(payload), version, entrypoint
    if kind == "turn_context" and isinstance(payload, dict):
        settings = (
            (payload.get("collaboration_mode") or {}).get("settings") if isinstance(payload.get("collaboration_mode"), dict) else None
        )
        return kind, _sorted_keys(payload) + _nested_keys(settings, "collaboration_mode.settings"), version, entrypoint
    return kind, _sorted_keys(payload) or _sorted_keys(entry), version, entrypoint


SHAPERS = {"claude": claude_shape, "codex": codex_shape}


def iter_files(paths: list[str], provider: str) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            files.extend(sorted(path.rglob("*.jsonl")))
        elif path.is_file():
            files.append(path)
    return files


def census(files: list[Path], provider: str) -> dict[str, dict[str, Any]]:
    shaper = SHAPERS[provider]
    shapes: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "files": set(), "versions": set(), "entrypoints": set(), "key_counts": defaultdict(int)}
    )
    for path in files:
        version = None
        entrypoint = None
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        record = shapes["<unparseable>"]
                        record["count"] += 1
                        record["files"].add(str(path))
                        continue
                    if not isinstance(entry, dict):
                        continue
                    shape, keys, seen_version, seen_entrypoint = shaper(entry)
                    version = seen_version or version
                    entrypoint = seen_entrypoint or entrypoint
                    record = shapes[shape]
                    record["count"] += 1
                    record["files"].add(str(path))
                    if version:
                        record["versions"].add(version)
                    if entrypoint:
                        record["entrypoints"].add(entrypoint)
                    for key in keys:
                        record["key_counts"][key] += 1
        except OSError:
            continue
    out: dict[str, dict[str, Any]] = {}
    for shape, record in sorted(shapes.items()):
        versions = sorted(record["versions"], key=_version_key)
        out[shape] = {
            "count": record["count"],
            "files": len(record["files"]),
            "first_seen_version": versions[0] if versions else None,
            "last_seen_version": versions[-1] if versions else None,
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
    catalog_path(provider).write_text(json.dumps(catalog, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def unclassified(catalog: dict[str, Any], observed: dict[str, dict[str, Any]]) -> list[str]:
    rows = catalog.get("shapes", {})
    return sorted(
        shape
        for shape in observed
        if shape not in rows or not str(rows[shape].get("classification") or "").strip() or rows[shape]["classification"] == "unclassified"
    )


def drift(catalog: dict[str, Any], observed: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Catalogued shapes observed without a key the parser requires.

    A signal's parser reads specific keys (`durationMs`, `info.last_token_usage.total_tokens`).
    A provider that renames or drops one keeps the discriminator, so only the
    key set can say the fact will silently stop. Reported per shape with how
    many of its lines lacked each key.
    """
    rows = catalog.get("shapes", {})
    found: dict[str, dict[str, Any]] = {}
    for shape, record in observed.items():
        row = rows.get(shape)
        if row is None:
            continue
        required = [key for key in (row.get("required_keys") or []) if isinstance(key, str)]
        if not required:
            continue
        key_counts = record.get("key_counts") or {}
        missing = {key: record["count"] - int(key_counts.get(key, 0)) for key in required if int(key_counts.get(key, 0)) < record["count"]}
        if missing:
            found[shape] = {"missing": missing, "lines": record["count"]}
    return found


def widened(catalog: dict[str, Any], observed: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Catalogued shapes carrying keys the catalog has never recorded: something new to classify."""
    rows = catalog.get("shapes", {})
    found: dict[str, list[str]] = {}
    for shape, record in observed.items():
        row = rows.get(shape)
        if row is None:
            continue
        new_keys = sorted(set(record.get("keys") or []) - set(row.get("keys") or []))
        if new_keys:
            found[shape] = new_keys
    return found


def seed(catalog: dict[str, Any], observed: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Append unseen shapes as unclassified; widen key sets and version ranges. Never delete."""
    rows = catalog.setdefault("shapes", {})
    added = widened_rows = 0
    for shape, record in observed.items():
        row = rows.get(shape)
        if row is None:
            rows[shape] = {
                "classification": "unclassified",
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
            if seen and (not row.get(bound) or pick(row[bound], seen, key=_version_key) != row[bound]):
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("census", "check", "seed"))
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, required=True)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json", action="store_true", help="census: emit JSON instead of a table")
    args = parser.parse_args(argv)

    files = iter_files(args.paths, args.provider)
    if not files:
        print("no transcript files found", file=sys.stderr)
        return 2
    observed = census(files, args.provider)

    if args.command == "census":
        if args.json:
            print(json.dumps({"provider": args.provider, "files": len(files), "shapes": observed}, indent=1))
        else:
            print(f"{args.provider}: {len(files)} files, {len(observed)} shapes")
            for shape, record in sorted(observed.items(), key=lambda item: -item[1]["count"]):
                print(
                    f"{record['count']:>8} {record['files']:>5} {record['first_seen_version'] or '-':<9} {record['last_seen_version'] or '-':<9} {','.join(record['entrypoints']) or '-':<14} {shape}"
                )
        return 0

    catalog = load_catalog(args.provider)
    if args.command == "seed":
        added, widened_rows = seed(catalog, observed)
        save_catalog(args.provider, catalog)
        print(f"{args.provider}: {added} shapes added, {widened_rows} rows widened, {len(catalog['shapes'])} total")
        return 0

    missing = unclassified(catalog, observed)
    drifted = drift(catalog, observed)
    new_keys = widened(catalog, observed)
    if missing:
        print(f"{args.provider}: {len(missing)} unclassified shape(s) in {len(files)} file(s):")
        for shape in missing:
            record = observed[shape]
            print(
                f"  {shape}  count={record['count']} versions={record['first_seen_version']}..{record['last_seen_version']} keys={','.join(record['keys'][:12])}"
            )
    if drifted:
        print(f"{args.provider}: {len(drifted)} shape(s) missing a key the parser requires:")
        for shape, report in drifted.items():
            gaps = ", ".join(f"{key} absent in {count} of {report['lines']} lines" for key, count in report["missing"].items())
            print(f"  {shape}  {gaps}")
    if new_keys:
        print(f"{args.provider}: {len(new_keys)} shape(s) carry keys the catalog has never seen (seed to accept):")
        for shape, keys in new_keys.items():
            print(f"  {shape}  +{','.join(keys)}")
    if missing or drifted or new_keys:
        return 1
    print(f"{args.provider}: {len(observed)} shapes, all classified, every required key present, no new keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
