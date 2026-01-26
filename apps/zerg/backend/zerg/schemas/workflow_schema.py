"""
Canonical workflow schema definitions.

This module defines the single source of truth for workflow data structures.
All internal processing should use these schemas.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Union

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class WorkflowNode(BaseModel):
    """Canonical representation of a workflow node."""

    model_config = ConfigDict(extra="allow")  # Allow additional fields for flexibility

    node_id: str = Field(..., description="Unique node identifier")
    node_type: Union[str, Dict[str, Any]] = Field(..., description="Node type configuration")
    position: Dict[str, float] = Field(default_factory=dict, description="Node position on canvas")
    config: Dict[str, Any] = Field(default_factory=dict, description="Node-specific configuration")


class WorkflowEdge(BaseModel):
    """Canonical representation of a workflow edge."""

    model_config = ConfigDict(extra="allow")  # Allow additional fields for flexibility

    from_node_id: str = Field(..., description="Source node ID")
    to_node_id: str = Field(..., description="Target node ID")
    config: Dict[str, Any] = Field(default_factory=dict, description="Edge-specific configuration")


class WorkflowCanvas(BaseModel):
    """Canonical representation of complete workflow canvas data."""

    model_config = ConfigDict(extra="allow")  # Allow additional fields for flexibility

    nodes: List[WorkflowNode] = Field(default_factory=list, description="List of workflow nodes")
    edges: List[WorkflowEdge] = Field(default_factory=list, description="List of workflow edges")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Canvas metadata")

    def get_node_by_id(self, node_id: str) -> WorkflowNode | None:
        """Get node by ID, return None if not found."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def get_node_ids(self) -> set[str]:
        """Get all node IDs as a set."""
        return {node.node_id for node in self.nodes}

    def get_edges_from_node(self, node_id: str) -> List[WorkflowEdge]:
        """Get all edges originating from the specified node."""
        return [edge for edge in self.edges if edge.from_node_id == node_id]

    def get_edges_to_node(self, node_id: str) -> List[WorkflowEdge]:
        """Get all edges targeting the specified node."""
        return [edge for edge in self.edges if edge.to_node_id == node_id]


# ==============================================================================
# INPUT FORMAT SCHEMAS - Handle external data formats cleanly
# ==============================================================================


class FrontendNode(BaseModel):
    """Frontend node format with field aliases for clean transformation."""

    model_config = ConfigDict(
        extra="allow",  # Preserve additional fields
        populate_by_name=True,  # Allow both "id" and "node_id"
    )

    # Primary fields with aliases to handle both naming conventions
    node_id: str = Field(alias="id")
    node_type: Union[str, Dict[str, Any]] = Field(alias="type")
    position: Dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def handle_field_variations(cls, values):
        """Handle multiple field name variations cleanly."""
        if not isinstance(values, dict):
            return values

        # Handle node_id variations
        if "id" not in values and "node_id" in values:
            values["id"] = values["node_id"]
        elif "id" not in values and "node_id" not in values:
            # Generate fallback if neither exists
            values["id"] = f"node_{id(values)}"

        # Handle node_type variations
        if "type" not in values and "node_type" in values:
            values["type"] = values["node_type"]
        elif "type" not in values and "node_type" not in values:
            values["type"] = "unknown"

        return values


# Legacy string node format removed - envelope format only


class FrontendEdge(BaseModel):
    """Frontend edge format with field aliases."""

    model_config = ConfigDict(
        extra="allow",  # Preserve additional fields
        populate_by_name=True,  # Allow both naming conventions
    )

    from_node_id: str = Field(alias="source")
    to_node_id: str = Field(alias="target")

    @model_validator(mode="before")
    @classmethod
    def handle_field_variations(cls, values):
        """Handle multiple edge field name variations."""
        if not isinstance(values, dict):
            return values

        # Handle from_node_id variations
        if "source" not in values:
            if "from_node_id" in values:
                values["source"] = values["from_node_id"]
            elif "from" in values:
                values["source"] = values["from"]

        # Handle to_node_id variations
        if "target" not in values:
            if "to_node_id" in values:
                values["target"] = values["to_node_id"]
            elif "to" in values:
                values["target"] = values["to"]

        return values


class InputCanvas(BaseModel):
    """Flexible input canvas that handles multiple external formats."""

    model_config = ConfigDict(extra="allow")

    nodes: List[Union[Dict[str, Any], str]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# NODE TYPE DISCRIMINATED UNIONS - Eliminate isinstance() code smell
# ==============================================================================


class ToolNodeType(BaseModel):
    """Tool node type configuration."""

    tool_name: str = Field("", description="Name of the tool to execute")
    config: Dict[str, Any] = Field(default_factory=dict, description="Tool configuration")
    static_params: Dict[str, Any] = Field(default_factory=dict, description="Static parameters")


class FicheNodeType(BaseModel):
    """Fiche node type configuration."""

    fiche_id: int = Field(0, description="ID of the fiche to execute")
    message: str = Field("Execute this task", description="Message to send to fiche")
    config: Dict[str, Any] = Field(default_factory=dict, description="Fiche configuration")


class TriggerNodeType(BaseModel):
    """Trigger node type configuration."""

    trigger_type: str = Field("webhook", description="Type of trigger (deprecated; use config.trigger in WorkflowData)")
    config: Dict[str, Any] = Field(default_factory=dict, description="Trigger configuration")


class NodeTypeHelper:
    """Helper class to handle node type variations without isinstance() checks."""

    @staticmethod
    def parse_node_type(
        node_type_raw: Union[str, Dict[str, Any]],
    ) -> tuple[str, Union[ToolNodeType, FicheNodeType, TriggerNodeType, None]]:
        """
        Parse node type and return (type_name, typed_config).

        Replaces all isinstance(node_type_raw, dict) patterns in the codebase.
        """
        if isinstance(node_type_raw, dict):
            # Handle frontend format: {"Tool": {...}}, {"Fiche": {...}}, etc.
            if "Tool" in node_type_raw:
                config_data = node_type_raw["Tool"] if isinstance(node_type_raw["Tool"], dict) else {}
                return "tool", ToolNodeType(**config_data)
            elif "Fiche" in node_type_raw:
                config_data = node_type_raw["Fiche"] if isinstance(node_type_raw["Fiche"], dict) else {}
                return "fiche", FicheNodeType(**config_data)
            elif "Trigger" in node_type_raw:
                config_data = node_type_raw["Trigger"] if isinstance(node_type_raw["Trigger"], dict) else {}
                return "trigger", TriggerNodeType(**config_data)
            else:
                # Unknown dict format, extract first key
                first_key = list(node_type_raw.keys())[0] if node_type_raw else "unknown"
                return first_key.lower(), None
        else:
            # Handle string format
            return str(node_type_raw).lower(), None

    @staticmethod
    def is_tool_type(node_type_raw: Union[str, Dict[str, Any]]) -> bool:
        """Check if node type is a tool."""
        type_name, _ = NodeTypeHelper.parse_node_type(node_type_raw)
        return type_name == "tool"

    @staticmethod
    def is_fiche_type(node_type_raw: Union[str, Dict[str, Any]]) -> bool:
        """Check if node type is a fiche."""
        type_name, _ = NodeTypeHelper.parse_node_type(node_type_raw)
        return type_name in ["fiche", "ficheidentity"]

    @staticmethod
    def is_trigger_type(node_type_raw: Union[str, Dict[str, Any]]) -> bool:
        """Check if node type is a trigger."""
        type_name, _ = NodeTypeHelper.parse_node_type(node_type_raw)
        return type_name == "trigger"
