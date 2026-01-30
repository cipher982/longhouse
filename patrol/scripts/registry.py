#!/usr/bin/env python3
"""
Registry for tracking patrol scans.
Prevents re-scanning the same targets.
"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

REGISTRY_FILE = Path(__file__).parent.parent / "registry" / "scans.jsonl"
DEFAULT_TTL_DAYS = 7


def _hash_target(target: str, prompt_id: str) -> str:
    """Create unique hash for target+prompt combo."""
    return hashlib.sha256(f"{prompt_id}:{target}".encode()).hexdigest()[:16]


def was_recently_scanned(target: str, prompt_id: str, ttl_days: int = DEFAULT_TTL_DAYS) -> bool:
    """Check if target was scanned by this prompt within TTL."""
    if not REGISTRY_FILE.exists():
        return False

    target_hash = _hash_target(target, prompt_id)
    cutoff = datetime.now() - timedelta(days=ttl_days)

    with open(REGISTRY_FILE) as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("hash") == target_hash:
                scan_time = datetime.fromisoformat(entry["timestamp"])
                if scan_time > cutoff:
                    return True
    return False


def record_scan(
    target: str,
    prompt_id: str,
    outcome: str,  # "finding" | "no_findings" | "invalid"
    evidence_hash: str | None = None,
):
    """Record a completed scan."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "hash": _hash_target(target, prompt_id),
        "target": target,
        "prompt_id": prompt_id,
        "outcome": outcome,
        "evidence_hash": evidence_hash,
        "timestamp": datetime.now().isoformat(),
    }

    with open(REGISTRY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_recent_scans(days: int = 7) -> list[dict]:
    """Get all scans within the last N days."""
    if not REGISTRY_FILE.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    results = []

    with open(REGISTRY_FILE) as f:
        for line in f:
            entry = json.loads(line)
            scan_time = datetime.fromisoformat(entry["timestamp"])
            if scan_time > cutoff:
                results.append(entry)

    return results


def stats() -> dict:
    """Get registry statistics."""
    scans = get_recent_scans(days=30)
    return {
        "total_scans": len(scans),
        "findings": sum(1 for s in scans if s["outcome"] == "finding"),
        "no_findings": sum(1 for s in scans if s["outcome"] == "no_findings"),
        "invalid": sum(1 for s in scans if s["outcome"] == "invalid"),
        "unique_targets": len(set(s["target"] for s in scans)),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2))
    else:
        print(json.dumps(get_recent_scans(), indent=2))
