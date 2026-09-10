"""OMP adapter registration and native JSONL normalization for the harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import register_adapter
from zerg.services.provider_interaction_semantics import omp_agent_end_is_terminal


@register_adapter("omp")
class OmpHarnessAdapter(UniversalProviderAdapter):
    """Keep OMP's native archive contract separate from Pi's adapter."""

    def decode_normalize(self, package: EvidencePackage, fixture_path: Path) -> dict[str, Any]:
        result = super().decode_normalize(package, fixture_path)
        try:
            rows = [json.loads(line) for line in fixture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return result

        def is_terminal_agent_end(row: dict[str, Any]) -> bool:
            return omp_agent_end_is_terminal(row)

        headers = [row for row in rows if isinstance(row, dict) and row.get("type") == "session" and row.get("id")]
        agent_ends = [row for row in rows if isinstance(row, dict) and row.get("type") == "agent_end"]
        terminal_agent_ends = [row for row in agent_ends if is_terminal_agent_end(row)]
        assistant_messages = [
            (index, row)
            for index, row in enumerate(rows)
            if isinstance(row, dict)
            and row.get("type") == "message"
            and isinstance(row.get("message"), dict)
            and row["message"].get("role") == "assistant"
        ]
        native = {
            "provider": "omp",
            "native_session_id": headers[0].get("id") if len(headers) == 1 else None,
            "session_header_count": len(headers),
            "assistant_message_count": len(assistant_messages),
            "agent_end_count": len(agent_ends),
            "terminal_agent_end_count": len(terminal_agent_ends),
            "native_archive_excludes_live_settlement": not agent_ends,
            "terminal_after_assistant": False,
            "agent_settled_is_not_completion_contract": True,
            "source": str(fixture_path),
        }
        package.write_json("assertions/omp-native-settlement.json", native)
        result["omp_native_settlement"] = native
        if len(headers) != 1 or not assistant_messages or agent_ends:
            result["status"] = "fail"
            result["failure_code"] = "omp_native_archive_shape_missing"
        return result
