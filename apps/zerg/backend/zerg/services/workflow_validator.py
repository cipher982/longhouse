"""
Clean Workflow Graph Validation Service.

Clean implementation that works with canonical WorkflowCanvas schema.
No defensive programming - assumes clean data format.
"""

import logging
from dataclasses import dataclass
from typing import List

from langgraph.checkpoint.memory import MemorySaver

from zerg.schemas.workflow_schema import NodeTypeHelper
from zerg.schemas.workflow_schema import WorkflowCanvas

logger = logging.getLogger(__name__)


@dataclass
class ValidationError:
    """Represents a validation error."""

    code: str
    message: str
    node_id: str = None
    severity: str = "error"  # error, warning


@dataclass
class ValidationResult:
    """Result of workflow validation."""

    is_valid: bool
    errors: List[ValidationError]
    warnings: List[ValidationError]

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


class WorkflowValidator:
    """
    Clean workflow validator that assumes canonical data format.
    No defensive programming needed - schema is guaranteed by Pydantic.
    """

    def __init__(self):
        self.max_nodes = 1000
        self.max_edges = 5000
        self.max_depth = 50

    def validate_workflow(self, canvas: WorkflowCanvas) -> ValidationResult:
        """
        Validate workflow using multi-layer approach:
        1. Structural validation
        2. LangGraph compilation validation
        3. Business logic validation
        """
        errors = []
        warnings = []

        try:
            # Layer 1: Structural validation
            structural_errors = self._validate_structure(canvas)
            errors.extend(structural_errors)

            # Layer 2: LangGraph compilation validation
            if not structural_errors:
                try:
                    langgraph_errors = self._validate_with_langgraph(canvas)
                    errors.extend(langgraph_errors)
                except Exception as e:
                    errors.append(ValidationError(code="LANGGRAPH_COMPILATION_FAILED", message=f"LangGraph compilation failed: {str(e)}"))

            # Layer 3: Business logic validation
            business_warnings = self._validate_business_logic(canvas)
            warnings.extend(business_warnings)

        except Exception as e:
            logger.exception("Unexpected error during workflow validation")
            errors.append(ValidationError(code="VALIDATION_EXCEPTION", message=f"Validation failed with exception: {str(e)}"))

        return ValidationResult(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _validate_structure(self, canvas: WorkflowCanvas) -> List[ValidationError]:
        """Validate basic structure and limits - clean implementation."""
        errors = []

        # Size limits
        if len(canvas.nodes) > self.max_nodes:
            errors.append(
                ValidationError(
                    code="TOO_MANY_NODES",
                    message=f"Workflow has {len(canvas.nodes)} nodes, maximum is {self.max_nodes}",
                )
            )

        if len(canvas.edges) > self.max_edges:
            errors.append(
                ValidationError(
                    code="TOO_MANY_EDGES",
                    message=f"Workflow has {len(canvas.edges)} edges, maximum is {self.max_edges}",
                )
            )

        # Node validation
        node_errors = self._validate_nodes(canvas)
        errors.extend(node_errors)

        # Edge validation
        edge_errors = self._validate_edges(canvas)
        errors.extend(edge_errors)

        # Cycle detection moved to business logic validation (warnings)

        return errors

    def _validate_nodes(self, canvas: WorkflowCanvas) -> List[ValidationError]:
        """Validate nodes - clean implementation."""
        errors = []
        node_ids = set()

        for node in canvas.nodes:
            # Check for duplicate node IDs
            if node.node_id in node_ids:
                errors.append(ValidationError(code="DUPLICATE_NODE_ID", message=f"Duplicate node ID: {node.node_id}", node_id=node.node_id))
            node_ids.add(node.node_id)

            # Validate node type using clean NodeTypeHelper
            node_type, typed_config = NodeTypeHelper.parse_node_type(node.node_type)

            if node_type == "tool":
                # Extract tool config - handle both typed config and dict format
                if typed_config:
                    # Use typed config object
                    tool_errors = self._validate_tool_node_typed(node.node_id, typed_config)
                else:
                    # Fallback to dictionary format
                    tool_config = node.node_type.get("Tool", {}) if isinstance(node.node_type, dict) else {}
                    tool_errors = self._validate_tool_node(node.node_id, tool_config)
                errors.extend(tool_errors)
            elif node_type in ["agent", "agentidentity"]:
                agent_errors = self._validate_agent_node(node.node_id, node)
                errors.extend(agent_errors)

        return errors

    def _validate_tool_node(self, node_id: str, tool_config: dict) -> List[ValidationError]:
        """Validate tool node configuration (dictionary format)."""
        errors = []

        tool_name = tool_config.get("tool_name")
        if not tool_name:
            errors.append(ValidationError(code="MISSING_TOOL_NAME", message=f"Tool node {node_id} missing tool_name", node_id=node_id))
            return errors

        # Validate tool exists using tool contracts
        return self._validate_tool_exists(node_id, tool_name)

    def _validate_tool_node_typed(self, node_id: str, tool_config) -> List[ValidationError]:
        """Validate tool node configuration (typed ToolNodeType format)."""
        errors = []

        tool_name = tool_config.tool_name
        if not tool_name:
            errors.append(ValidationError(code="MISSING_TOOL_NAME", message=f"Tool node {node_id} missing tool_name", node_id=node_id))
            return errors

        # Validate tool exists using tool contracts
        return self._validate_tool_exists(node_id, tool_name)

    def _validate_tool_exists(self, node_id: str, tool_name: str) -> List[ValidationError]:
        """Validate that the specified tool exists in our tool registry."""
        errors = []

        # Validate against tool contracts
        if tool_name:
            try:
                from zerg.tools.unified_access import get_tool_resolver

                resolver = get_tool_resolver()
                if not resolver.has_tool(tool_name):
                    valid_tools = resolver.get_tool_names()
                    errors.append(
                        ValidationError(
                            code="INVALID_TOOL_NAME",
                            message=f"Tool node {node_id} uses invalid tool '{tool_name}'. Valid tools: {valid_tools}",
                            node_id=node_id,
                        )
                    )
            except ImportError:
                # Tool definitions not available - skip validation
                pass

        return errors

    def _validate_agent_node(self, node_id: str, node) -> List[ValidationError]:
        """Validate agent node configuration."""
        errors = []

        # Check for required agent_id in config
        agent_id = None
        if hasattr(node, "config") and node.config:
            agent_id = node.config.get("agent_id")

        if not agent_id:
            errors.append(
                ValidationError(code="MISSING_AGENT_ID", message=f"Agent node {node_id} missing required agent_id", node_id=node_id)
            )

        return errors

    def _validate_edges(self, canvas: WorkflowCanvas) -> List[ValidationError]:
        """Validate edges between nodes."""
        errors = []
        node_ids = canvas.get_node_ids()

        for edge in canvas.edges:
            if edge.from_node_id not in node_ids:
                errors.append(ValidationError(code="INVALID_EDGE_SOURCE", message=f"Edge source node '{edge.from_node_id}' does not exist"))

            if edge.to_node_id not in node_ids:
                errors.append(ValidationError(code="INVALID_EDGE_TARGET", message=f"Edge target node '{edge.to_node_id}' does not exist"))

        return errors

    def _validate_with_langgraph(self, canvas: WorkflowCanvas) -> List[ValidationError]:
        """Validate by attempting LangGraph compilation."""
        errors = []

        try:
            from typing import TypedDict

            from langgraph.graph import StateGraph

            # Simple state for validation
            class TestState(TypedDict):
                data: str

            workflow = StateGraph(TestState)

            # Add nodes - clean, no defensive programming
            for node in canvas.nodes:
                workflow.add_node(node.node_id, lambda x: x)

            # Add edges - clean, no defensive programming
            for edge in canvas.edges:
                workflow.add_edge(edge.from_node_id, edge.to_node_id)

            # Try to compile
            checkpointer = MemorySaver()
            workflow.compile(checkpointer=checkpointer)

        except Exception as e:
            errors.append(ValidationError(code="LANGGRAPH_VALIDATION_FAILED", message=f"LangGraph validation failed: {str(e)}"))

        return errors

    def _validate_business_logic(self, canvas: WorkflowCanvas) -> List[ValidationError]:
        """Business logic validation - returns warnings."""
        warnings = []

        # Check for trigger nodes using clean NodeTypeHelper
        has_trigger = any(NodeTypeHelper.is_trigger_type(node.node_type) for node in canvas.nodes)

        if not has_trigger:
            warnings.append(
                ValidationError(
                    code="NO_TRIGGER_NODE",
                    message="Workflow has no trigger nodes - may not execute automatically",
                    severity="warning",
                )
            )

        # Check for orphaned nodes
        orphaned = self._find_orphaned_nodes(canvas)
        for orphaned_node in orphaned:
            warnings.append(
                ValidationError(
                    code="ORPHANED_NODE",
                    message=f"Node '{orphaned_node}' is not connected to workflow",
                    node_id=orphaned_node,
                    severity="warning",
                )
            )

        # Cycle detection (only for reasonable sizes)
        if len(canvas.nodes) <= 500:
            cycles = self._detect_cycles(canvas)
            if cycles:
                warnings.append(
                    ValidationError(
                        code="POTENTIAL_CYCLE",
                        message=f"Workflow contains cycles: {', '.join(cycles[:3])}",
                        severity="warning",
                    )
                )

        return warnings

    def _find_orphaned_nodes(self, canvas: WorkflowCanvas) -> List[str]:
        """Find nodes that are not connected to anything."""
        node_ids = canvas.get_node_ids()
        connected_ids = set()

        for edge in canvas.edges:
            connected_ids.add(edge.from_node_id)
            connected_ids.add(edge.to_node_id)

        return list(node_ids - connected_ids)

    def _detect_cycles(self, canvas: WorkflowCanvas) -> List[str]:
        """Simple cycle detection using iterative DFS."""
        # Build adjacency list
        graph = {node.node_id: [] for node in canvas.nodes}

        for edge in canvas.edges:
            if edge.from_node_id in graph:
                graph[edge.from_node_id].append(edge.to_node_id)

        # DFS cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node_id: WHITE for node_id in graph}
        cycles = []

        def has_cycle_from(start):
            stack = [(start, iter(graph.get(start, [])))]
            path = []

            while stack:
                node, children = stack[-1]
                if color[node] == WHITE:
                    color[node] = GRAY
                    path.append(node)

                try:
                    child = next(children)
                    if child in graph:
                        if color[child] == GRAY:
                            # Found cycle
                            cycle_start = path.index(child)
                            cycle_path = path[cycle_start:] + [child]
                            cycles.append(" -> ".join(cycle_path))
                            return True
                        elif color[child] == WHITE:
                            stack.append((child, iter(graph.get(child, []))))
                except StopIteration:
                    # No more children
                    stack.pop()
                    if path and path[-1] == node:
                        path.pop()
                        color[node] = BLACK

            return False

        for node_id in graph:
            if color[node_id] == WHITE:
                if has_cycle_from(node_id):
                    break  # Found at least one cycle

        return cycles[:3]  # Limit to first 3 cycles
