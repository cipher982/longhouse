"""OMP adapter registration and native JSONL normalization for the harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import register_adapter


@register_adapter("omp")
class OmpHarnessAdapter(UniversalProviderAdapter):
    """Keep OMP's native archive contract separate from Pi's adapter."""

    def decode_normalize(self, package: EvidencePackage, fixture_path: Path) -> dict[str, Any]:
        result = super().decode_normalize(package, fixture_path)
        try:
            rows = [json.loads(line) for line in fixture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return result
        headers = [row for row in rows if isinstance(row, dict) and row.get("type") == "session" and row.get("id")]
        agent_ends = [row for row in rows if isinstance(row, dict) and row.get("type") == "agent_end"]
        terminal_agent_ends = [row for row in agent_ends if row.get("isTerminal") is True or row.get("willContinue") is False]
        native = {
            "provider": "omp",
            "native_session_id": headers[0].get("id") if headers else None,
            "session_header_count": len(headers),
            "agent_end_count": len(agent_ends),
            "terminal_agent_end_count": len(terminal_agent_ends),
            "agent_settled_is_not_completion_contract": True,
            "source": str(fixture_path),
        }
        package.write_json("assertions/omp-native-settlement.json", native)
        result["omp_native_settlement"] = native
        if not headers or not terminal_agent_ends:
            result["status"] = "fail"
            result["failure_code"] = "omp_terminal_agent_end_missing"
        return result
