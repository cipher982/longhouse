#!/usr/bin/env python3
"""Census of provider transcript shapes, checked against the shape catalog.

Providers publish no list of what their terminal renders. The only way to
notice a new or vanished transcript line is to fingerprint what they write
and compare it with what we have catalogued. A shape is the provider plus
the entrypoint plus the full discriminator path (`system/turn_duration`,
`attachment/hook_success`, `event_msg/item_completed/CommandExecution`),
never just the top-level type.

    transcript_census.py census  --provider claude PATH [PATH ...]
        Fingerprint transcripts (files or directories) and print the census.
    transcript_census.py check   --provider claude PATH [PATH ...]
        Exit 1 if any observed shape is not classified in the catalog.
    transcript_census.py seed    --provider claude PATH [PATH ...]
        Add every unseen shape to the catalog as `unclassified` and extend
        first/last seen versions. Rows are never removed.

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


def _version_key(value: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in value.split("."))


def claude_shape(entry: dict) -> tuple[str, list[str], str | None, str | None]:
    """(shape, keys, version, entrypoint) for one Claude Code JSONL line."""
    kind = str(entry.get("type", "?"))
    version = entry.get("version") if isinstance(entry.get("version"), str) else None
    entrypoint = entry.get("entrypoint") if isinstance(entry.get("entrypoint"), str) else None
    if kind == "system":
        return f"system/{entry.get('subtype', '?')}", _sorted_keys(entry), version, entrypoint
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
        return shape, keys, version, entrypoint
    if kind == "response_item" and isinstance(payload, dict):
        role = payload.get("role")
        shape = f"response_item/{payload.get('type', '?')}" + (f"/{role}" if role else "")
        return shape, _sorted_keys(payload), version, entrypoint
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
        lambda: {"count": 0, "files": set(), "versions": set(), "entrypoints": set(), "keys": None}
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
                    if record["keys"] is None:
                        record["keys"] = keys
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
            "keys": record["keys"] or [],
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


def seed(catalog: dict[str, Any], observed: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Append unseen shapes as unclassified; widen version ranges. Never delete."""
    rows = catalog.setdefault("shapes", {})
    added = widened = 0
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
        widened += int(changed)
    return added, widened


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
        added, widened = seed(catalog, observed)
        save_catalog(args.provider, catalog)
        print(f"{args.provider}: {added} shapes added, {widened} rows widened, {len(catalog['shapes'])} total")
        return 0

    missing = unclassified(catalog, observed)
    if missing:
        print(f"{args.provider}: {len(missing)} unclassified shape(s) in {len(files)} file(s):")
        for shape in missing:
            record = observed[shape]
            print(
                f"  {shape}  count={record['count']} versions={record['first_seen_version']}..{record['last_seen_version']} keys={','.join(record['keys'][:12])}"
            )
        return 1
    print(f"{args.provider}: {len(observed)} shapes, all classified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
