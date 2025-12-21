import type { Node as FlowNode, Edge } from "@xyflow/react";
import type { WorkflowData, WorkflowDataInput, WorkflowNode, WorkflowEdge } from "../../services/api";

// Type for node config data - properly typed to match backend schema
interface NodeConfig {
  text?: string;
  agent_id?: number;
  tool_type?: string;
  [key: string]: unknown; // Allow additional properties
}

// Convert backend WorkflowData to React Flow format
export function convertToReactFlowData(workflowData: WorkflowData): { nodes: FlowNode[]; edges: Edge[] } {
  const nodes: FlowNode[] = workflowData.nodes.map((node: WorkflowNode) => ({
    id: node.id,
    type: node.type,
    position: { x: node.position.x, y: node.position.y },
    data: {
      label: (node.config as NodeConfig)?.text || `${node.type} node`,
      agentId: (node.config as NodeConfig)?.agent_id,
      toolType: (node.config as NodeConfig)?.tool_type,
    },
  }));

  const edges: Edge[] = workflowData.edges.map((edge: WorkflowEdge) => ({
    id: `${edge.from_node_id}-${edge.to_node_id}`,
    source: edge.from_node_id,
    target: edge.to_node_id,
  }));

  return { nodes, edges };
}

// Normalize workflow data to eliminate float drift and ordering differences
export function normalizeWorkflow(nodes: FlowNode[], edges: Edge[]): WorkflowDataInput {
  const sortedNodes = [...nodes]
    .sort((a, b) => a.id.localeCompare(b.id))
    .map((node) => ({
      id: node.id,
      type: node.type as "agent" | "tool" | "trigger" | "conditional",
      position: {
        x: Math.round(node.position.x * 2) / 2, // 0.5px quantization
        y: Math.round(node.position.y * 2) / 2,
      },
      config: {
        text: node.data.label,
        agent_id: node.data.agentId,
        tool_type: node.data.toolType,
      },
    })) as unknown as WorkflowNode[];

  const sortedEdges = [...edges]
    .sort((a, b) => a.id.localeCompare(b.id))
    .map((edge) => ({
      from_node_id: edge.source,
      to_node_id: edge.target,
      config: {}, // Edges typically don't have config data
    }));

  return { nodes: sortedNodes, edges: sortedEdges };
}

// Hash workflow data for change detection
export async function hashWorkflow(data: WorkflowDataInput): Promise<string> {
  const json = JSON.stringify(data);

  // Fallback for environments without crypto.subtle (tests, HTTP contexts)
  if (typeof crypto === "undefined" || !crypto.subtle) {
    return json; // Use JSON string as hash fallback
  }

  const buffer = new TextEncoder().encode(json);
  const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
}
