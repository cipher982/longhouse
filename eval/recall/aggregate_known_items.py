"""Summarize run_known_items.py reports: recall by category, errors and latency."""

import collections
import json
import sys

for path in sys.argv[1:]:
    report = json.load(open(path))
    for phase in ("cold", "warm"):
        block = report[phase]
        totals = collections.Counter()
        hits = collections.Counter()
        for key, value in block["by_provider_category"].items():
            category = key.split("/", 1)[1]
            totals[category] += value["total"]
            hits[category] += value["hits"]
        total, hit = sum(totals.values()), sum(hits.values())
        latency = block.get("latency_seconds", {})
        print(
            f"{path} {phase}: {hit}/{total} = {hit / max(total, 1):.3f} errors={block['errors']} "
            f"p50={latency.get('p50', 0):.3f} p95={latency.get('p95', 0):.3f} p99={latency.get('p99', 0):.3f}"
        )
        if phase == "cold":
            print("  ", {category: f"{hits[category]}/{totals[category]}" for category in totals})
