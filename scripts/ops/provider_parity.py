#!/usr/bin/env python3
"""Print the 3-stage transcript signal parity matrix from the canonical contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from zerg.managed_provider_contract_manifest import TRANSCRIPT_SIGNAL_IDS  # noqa: E402
from zerg.managed_provider_contract_manifest import TURN_USAGE_FIELD_NAMES  # noqa: E402
from zerg.managed_provider_contract_manifest import managed_provider_contract_manifest  # noqa: E402
from zerg.services.managed_provider_contracts import outstanding_factory_work  # noqa: E402

_DISPOSITION_LABEL = {
    "implemented": "impl",
    "not_implemented": "gap",
    "upstream_absent": "n/a",
    "policy_disabled": "off",
}


def build_parity_report() -> dict[str, object]:
    manifest = managed_provider_contract_manifest()
    providers = list(manifest.get("providers") or ())
    surfaces = dict(manifest.get("transcript_signal_surfaces") or {})
    open_work = [row for row in outstanding_factory_work() if row["operation"].startswith("transcript_signal")]
    return {
        "providers": [str(item["provider"]) for item in providers],
        "signals": {
            signal: {
                "surface": surfaces.get(signal, {}),
                "providers": {
                    str(item["provider"]): dict((item.get("transcript_signals") or {}).get(signal) or {})
                    for item in providers
                },
            }
            for signal in sorted(TRANSCRIPT_SIGNAL_IDS)
        },
        "turn_usage_fields": {
            str(item["provider"]): dict(((item.get("transcript_signals") or {}).get("turn.usage") or {}).get("fields") or {})
            for item in providers
        },
        "open_work": open_work,
    }


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a formatted table")
    args = parser.parse_args()

    report = build_parity_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    provider_names: list[str] = list(report["providers"])  # type: ignore[arg-type]
    signals: dict[str, dict[str, object]] = report["signals"]  # type: ignore[assignment]

    signal_rows: list[list[str]] = []
    for signal in sorted(TRANSCRIPT_SIGNAL_IDS):
        entry = signals[signal]
        surface = dict(entry["surface"])  # type: ignore[arg-type]
        surface_label = _DISPOSITION_LABEL.get(str(surface.get("disposition") or ""), "?")
        by_provider: dict[str, dict[str, object]] = entry["providers"]  # type: ignore[assignment]
        row = [signal, surface_label]
        for provider in provider_names:
            disp = str((by_provider.get(provider) or {}).get("disposition") or "?")
            row.append(_DISPOSITION_LABEL.get(disp, "?"))
        signal_rows.append(row)

    print("Transcript Signal Parity (Stage 1+2 per provider; Stage 3 Surface across all)")
    print("Legend: impl=ingested/surfaced  gap=upstream supports, not implemented  n/a=upstream absent  off=policy disabled\n")
    print(_render_table(["signal", "surface", *provider_names], signal_rows))

    usage_fields: dict[str, dict[str, str]] = report["turn_usage_fields"]  # type: ignore[assignment]
    usage_rows: list[list[str]] = []
    for provider in provider_names:
        fields = usage_fields.get(provider) or {}
        usage_rows.append([provider, *(str(fields.get(name) or "-") for name in TURN_USAGE_FIELD_NAMES)])

    print("\nTurn Usage Field Provenance (for implemented turn.usage providers)")
    print(_render_table(["provider", *TURN_USAGE_FIELD_NAMES], usage_rows))

    open_work: list[dict[str, str]] = list(report["open_work"])  # type: ignore[arg-type]
    ingest_gaps = [row for row in open_work if row["operation"].startswith("transcript_signal:")]
    surface_gaps = [row for row in open_work if row["operation"].startswith("transcript_signal_surface:")]
    print(f"\nOpen transcript signal gaps: {len(ingest_gaps)} ingest, {len(surface_gaps)} surface")
    for row in surface_gaps:
        print(f"  - {row['operation']}: {row['owner_action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
