"""
Single source of truth for workflow data structures.

This module defines the WorkflowData schema used across API, DB, and execution engine.
Replaces the old canonical/transformer/serializer stack.
"""

from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator


# Workflow Execution Response Models
class ExecutionStatusResponse(BaseModel):
    """Response for workflow execution status."""
    execution_id: int
    phase: str
    result: Optional[Any] = None


class ExecutionLogsResponse(BaseModel):
    """Response for workflow execution logs."""
    logs: str


class Position(BaseModel):
    """Node position on canvas."""

    x: float
    y: float


class WorkflowNode(BaseModel):
    """A workflow node (agent, tool, trigger, or conditional)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["agent", "tool", "trigger", "conditional"]
    position: Position
    config: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_trigger_config(cls, data: Any):
        """Upgrade legacy trigger config to typed shape to preserve old workflows.

        - Accept payloads that still use legacy keys (trigger_type, enabled, params, filters)
        - Fill in a default manual trigger when no semantic trigger metadata exists yet
        """

        if not isinstance(data, dict) or data.get("type") != "trigger":
            return data

        raw_config = data.get("config")
        config = raw_config if isinstance(raw_config, dict) else {}

        trigger_meta = config.get("trigger") if isinstance(config.get("trigger"), dict) else None
        if trigger_meta is not None:
            # Already in new shape – ensure legacy keys are stripped if present.
            cleaned_config = {
                key: value
                for key, value in config.items()
                if key not in {"trigger_type", "enabled", "params", "filters"}
            }
            cleaned_config["trigger"] = trigger_meta
            return {**data, "config": cleaned_config}

        legacy_type = config.get("trigger_type")
        inferred_type = legacy_type.lower() if isinstance(legacy_type, str) else None

        if inferred_type is None:
            # Attempt to infer from visual title; fall back to manual trigger.
            label = str(config.get("text", "")).lower()
            if "email" in label:
                inferred_type = "email"
            elif "schedule" in label or "cron" in label:
                inferred_type = "schedule"
            elif "webhook" in label:
                inferred_type = "webhook"
            else:
                inferred_type = "manual"

        legacy_params = config.get("params") if isinstance(config.get("params"), dict) else {}
        legacy_filters = config.get("filters") if isinstance(config.get("filters"), list) else []
        legacy_enabled = config.get("enabled") if isinstance(config.get("enabled"), bool) else True

        upgraded = {
            key: value for key, value in config.items() if key not in {"trigger_type", "enabled", "params", "filters"}
        }
        upgraded["trigger"] = {
            "type": inferred_type,
            "config": {
                "enabled": legacy_enabled,
                "params": legacy_params,
                "filters": legacy_filters,
            },
        }

        return {**data, "config": upgraded}


class WorkflowEdge(BaseModel):
    """A directed edge connecting two nodes."""

    model_config = ConfigDict(
        populate_by_name=True,
        # Consistent field naming - no more aliasing confusion
        alias_generator=lambda field_name: field_name,
        extra="forbid",
    )

    from_node_id: str  # ✅ Consistent with frontend - no more "from" alias
    to_node_id: str  # ✅ Consistent with frontend - no more confusion
    config: Dict[str, Any] = Field(default_factory=dict)


class WorkflowData(BaseModel):
    """Complete workflow specification."""

    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge]

    @field_validator("edges")
    @classmethod
    def _check_edges(cls, edges, info):
        """Validate edge references to existing nodes."""
        if "nodes" not in info.data:
            return edges

        node_ids = {n.id for n in info.data["nodes"]}
        for e in edges:
            if e.from_node_id not in node_ids or e.to_node_id not in node_ids:
                raise ValueError(f"Edge {e.from_node_id}->{e.to_node_id} references unknown node")
        return edges

    @field_validator("nodes")
    @classmethod
    def _validate_triggers_and_manual_unique(cls, nodes):
        """Strict validation for trigger nodes + uniqueness of manual.

        - Require typed trigger meta at config.trigger with shape { type: str, config: { enabled, params, filters } }
        - Forbid legacy flattened keys (trigger_type, enabled, params, filters) at top-level config
        - Enforce at most one manual trigger (lowercase literal)
        """
        manual_count = 0
        for n in nodes:
            if n.type != "trigger":
                continue
            cfg = n.config or {}

            # Fail fast on legacy keys
            legacy_keys = {k for k in cfg.keys() if k in {"trigger_type", "enabled", "params", "filters"}}
            if legacy_keys:
                raise ValueError(f"Node '{n.id}': legacy trigger keys are not allowed: {sorted(legacy_keys)}")

            trig = cfg.get("trigger")
            if not isinstance(trig, dict):
                raise ValueError(f"Node '{n.id}': trigger node missing required config.trigger")

            ttype = trig.get("type")
            if not isinstance(ttype, str):
                raise ValueError(f"Node '{n.id}': config.trigger.type must be a string")
            if ttype != ttype.lower():
                raise ValueError(f"Node '{n.id}': config.trigger.type must be lowercase literal")

            # Uniqueness of manual
            if ttype == "manual":
                manual_count += 1
                if manual_count > 1:
                    raise ValueError("Only one Manual trigger is allowed per workflow")

            tconf = trig.get("config")
            if not isinstance(tconf, dict):
                raise ValueError(f"Node '{n.id}': config.trigger.config must be an object")

            # Optional fields
            if "enabled" in tconf and not isinstance(tconf.get("enabled"), bool):
                raise ValueError(f"Node '{n.id}': config.trigger.config.enabled must be boolean if present")
            if "params" in tconf and not isinstance(tconf.get("params"), dict):
                raise ValueError(f"Node '{n.id}': config.trigger.config.params must be object if present")
            if "filters" in tconf and not isinstance(tconf.get("filters"), list):
                raise ValueError(f"Node '{n.id}': config.trigger.config.filters must be array if present")

        return nodes

    model_config = ConfigDict(extra="forbid")  # Reject unknown fields for security


# ---------------------------------------------------------------------------
# Trigger meta resolver (strict typed-only)
# ---------------------------------------------------------------------------


def resolve_trigger_meta(node: WorkflowNode) -> dict:
    """Return canonical trigger meta for a trigger node (strict, typed-only)."""
    if node.type != "trigger":
        return {}
    cfg: dict = node.config or {}
    trig = cfg.get("trigger")
    if not isinstance(trig, dict):
        raise ValueError(f"Node '{node.id}': trigger node missing required config.trigger")
    if any(k in cfg for k in ("trigger_type", "enabled", "params", "filters")):
        raise ValueError(f"Node '{node.id}': legacy trigger keys present in node.config")
    ttype = trig.get("type")
    if not isinstance(ttype, str) or ttype != ttype.lower():
        raise ValueError(f"Node '{node.id}': config.trigger.type must be lowercase string")
    tconf = trig.get("config") if isinstance(trig.get("config"), dict) else {}
    enabled = bool(tconf.get("enabled", True))
    params = tconf.get("params") if isinstance(tconf.get("params"), dict) else {}
    filters = tconf.get("filters") if isinstance(tconf.get("filters"), list) else []
    return {"type": ttype, "config": {"enabled": enabled, "params": params, "filters": filters}}
