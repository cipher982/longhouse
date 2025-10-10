import React, { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "../lib/useWebSocket";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type Connection,
  type OnConnect,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "../styles/canvas-react.css";
import toast from "react-hot-toast";
import {
  fetchAgents,
  fetchCurrentWorkflow,
  updateWorkflowCanvas,
  startWorkflowExecution,
  getExecutionStatus,
  getExecutionLogs,
  cancelExecution,
  type AgentSummary,
  type Workflow,
  type WorkflowData,
  type WorkflowDataInput,
  type WorkflowNode,
  type WorkflowEdge,
  type ExecutionStatus,
} from "../services/api";

// Type for node config data - properly typed to match backend schema
interface NodeConfig {
  text?: string;
  agent_id?: number;
  tool_type?: string;
  [key: string]: unknown; // Allow additional properties
}

// Custom node component for agents
function AgentNode({ data }: { data: { label: string; agentId?: number } }) {
  return (
    <div className="agent-node">
      <div className="agent-icon">🤖</div>
      <div className="agent-name">{data.label}</div>
    </div>
  );
}

// Custom node component for tools
function ToolNode({ data }: { data: { label: string; toolType?: string } }) {
  const icon = data.toolType === 'http-request' ? '🌐' : data.toolType === 'url-fetch' ? '📡' : '🔧';

  return (
    <div className="tool-node">
      <div className="tool-icon">{icon}</div>
      <div className="tool-name">{data.label}</div>
    </div>
  );
}

// Custom node component for triggers
function TriggerNode({ data }: { data: { label: string } }) {
  return (
    <div className="trigger-node">
      <div className="trigger-icon">⚡</div>
      <div className="trigger-name">{data.label}</div>
    </div>
  );
}

const nodeTypes: NodeTypes = {
  agent: AgentNode,
  tool: ToolNode,
  trigger: TriggerNode,
};

// Convert backend WorkflowData to React Flow format
function convertToReactFlowData(workflowData: WorkflowData): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = workflowData.nodes.map((node: WorkflowNode) => ({
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

// Convert React Flow data to backend WorkflowData format
function convertToBackendFormat(nodes: Node[], edges: Edge[]): WorkflowDataInput {
  const workflowNodes = nodes.map((node) => ({
    id: node.id,
    type: node.type as "agent" | "tool" | "trigger" | "conditional",
    position: { x: node.position.x, y: node.position.y },
    config: {
      text: node.data.label,
      agent_id: node.data.agentId,
      tool_type: node.data.toolType,
    },
  })) as unknown as WorkflowNode[];

  const workflowEdges: WorkflowEdge[] = edges.map((edge) => ({
    from_node_id: edge.source,
    to_node_id: edge.target,
    config: {},
  }));

  return {
    nodes: workflowNodes,
    edges: workflowEdges,
  };
}

// Normalize workflow data to eliminate float drift and ordering differences
function normalizeWorkflow(nodes: Node[], edges: Edge[]): WorkflowDataInput {
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
async function hashWorkflow(data: WorkflowDataInput): Promise<string> {
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

export default function CanvasPage() {
  const queryClient = useQueryClient();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [isLoading, setIsLoading] = useState(false);
  const lastSavedHashRef = useRef<string>("");
  const pendingHashesRef = useRef<Set<string>>(new Set());
  const currentExecutionRef = useRef<ExecutionStatus | null>(null);
  const toastIdRef = useRef<string | null>(null);

  // Execution state
  const [currentExecution, setCurrentExecution] = useState<ExecutionStatus | null>(null);
  const [executionLogs, setExecutionLogs] = useState<string>("");
  const [isDragActive, setIsDragActive] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [showLogs, setShowLogs] = useState(false);

  // Fetch agents for the shelf
  const { data: agents = [] } = useQuery<AgentSummary[]>({
    queryKey: ["agents", { scope: "my" }],
    queryFn: () => fetchAgents({ scope: "my" }),
    refetchInterval: 2000, // Poll every 2 seconds
  });

  // Fetch current workflow
  const { data: workflow } = useQuery<Workflow>({
    queryKey: ["workflow", "current"],
    queryFn: fetchCurrentWorkflow,
    staleTime: 30000, // Consider data fresh for 30 seconds
  });

  // Initialize nodes and edges from workflow data
  React.useEffect(() => {
    if (workflow?.canvas) {
      const { nodes: flowNodes, edges: flowEdges } = convertToReactFlowData(workflow.canvas);
      setNodes(flowNodes);
      setEdges(flowEdges);

      // Initialize hash from loaded workflow
      const normalized = normalizeWorkflow(flowNodes, flowEdges);
      hashWorkflow(normalized).then((hash) => {
        lastSavedHashRef.current = hash;
      });
    }
  }, [workflow, setNodes, setEdges]);

  // Sync ref with latest execution for stable WebSocket handler
  useEffect(() => {
    currentExecutionRef.current = currentExecution;
  }, [currentExecution]);

  // Save workflow mutation with hash-based deduplication
  const saveWorkflowMutation = useMutation({
    onMutate: async (data: WorkflowDataInput) => {
      const hash = await hashWorkflow(data);
      return { hash };
    },
    mutationFn: async (data: WorkflowDataInput) => {
      const hash = await hashWorkflow(data);

      // Skip if identical to last saved OR already in flight
      if (hash === lastSavedHashRef.current || pendingHashesRef.current.has(hash)) {
        return null;
      }

      pendingHashesRef.current.add(hash);
      const result = await updateWorkflowCanvas(data);
      return result;
    },
    onSuccess: (result, _variables, context) => {
      if (!result || !context) return; // Skipped save

      lastSavedHashRef.current = context.hash;
      pendingHashesRef.current.delete(context.hash);

      // Reuse single toast ID to avoid stacking
      if (toastIdRef.current) {
        toast.success("Workflow saved", { id: toastIdRef.current });
      } else {
        toastIdRef.current = toast.success("Workflow saved");
      }

      queryClient.setQueryData(["workflow", "current"], result);
    },
    onError: (error: Error, _variables, context) => {
      if (context?.hash) {
        pendingHashesRef.current.delete(context.hash);
      }
      console.error("Failed to save workflow:", error);
      toast.error(`Failed to save workflow: ${error.message || "Unknown error"}`);
    },
  });

  // Auto-save workflow when nodes or edges change (skip during drag)
  const debouncedSave = useCallback(
    debounce((nodes: Node[], edges: Edge[]) => {
      if (nodes.length > 0 || edges.length > 0) {
        const workflowData = normalizeWorkflow(nodes, edges);
        saveWorkflowMutation.mutate(workflowData);
      }
    }, 1000),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [saveWorkflowMutation]
  );

  React.useEffect(() => {
    if (!isDragging) {
      debouncedSave(nodes, edges);
    }
  }, [nodes, edges, isDragging, debouncedSave]);

  // Workflow execution mutations
  const executeWorkflowMutation = useMutation({
    mutationFn: async () => {
      if (!workflow?.id) {
        throw new Error("No workflow loaded");
      }
      return startWorkflowExecution(workflow.id);
    },
    onSuccess: (execution) => {
      setCurrentExecution(execution);
      toast.success("Workflow execution started!");
      setShowLogs(true);
    },
    onError: (error: Error) => {
      toast.error(`Failed to start workflow: ${error.message || "Unknown error"}`);
    },
  });

  const cancelExecutionMutation = useMutation({
    mutationFn: async () => {
      if (!currentExecution?.execution_id) {
        throw new Error("No execution to cancel");
      }
      return cancelExecution(currentExecution.execution_id, "Cancelled by user");
    },
    onSuccess: () => {
      toast.success("Workflow execution cancelled");
      setCurrentExecution(null);
    },
    onError: (error: Error) => {
      toast.error(`Failed to cancel execution: ${error.message || "Unknown error"}`);
    },
  });

  // WebSocket for real-time execution updates
  const handleExecutionMessage = useCallback(async () => {
    const execution = currentExecutionRef.current;
    if (!execution?.execution_id) {
      return;
    }

    try {
      const updatedStatus = await getExecutionStatus(execution.execution_id);

      setCurrentExecution((prevExecution) => ({
        ...updatedStatus,
        execution_id: prevExecution?.execution_id || execution.execution_id,
      }));

      if (updatedStatus.phase === "finished" || updatedStatus.phase === "cancelled") {
        try {
          const logs = await getExecutionLogs(execution.execution_id);
          setExecutionLogs(logs.logs);
        } catch (error) {
          console.error("Failed to fetch execution logs:", error);
        }
      }
    } catch (error) {
      console.error("Failed to fetch execution status:", error);
    }
  }, [getExecutionLogs, getExecutionStatus]);

  useWebSocket(currentExecution?.execution_id != null, {
    includeAuth: true,
    invalidateQueries: [],
    onMessage: handleExecutionMessage,
  });

  // Handle connection creation
  const onConnect: OnConnect = useCallback(
    (connection: Connection) => {
      setEdges((eds: Edge[]) => addEdge(connection, eds));
    },
    [setEdges]
  );

  // Drag lifecycle handlers
  const onNodeDragStart = useCallback(() => {
    setIsDragging(true);
  }, []);

  const onNodeDragStop = useCallback(() => {
    setIsDragging(false);
    // Trigger immediate save after drag completes
    if (nodes.length > 0 || edges.length > 0) {
      const workflowData = normalizeWorkflow(nodes, edges);
      saveWorkflowMutation.mutate(workflowData);
    }
  }, [nodes, edges, saveWorkflowMutation]);

  // E2E Test Compatibility: Add legacy CSS classes to React Flow nodes
  useEffect(() => {
    const addCompatibilityClasses = () => {
      // Add .canvas-node, .generic-node classes to React Flow nodes for E2E tests
      const reactFlowNodes = document.querySelectorAll('.react-flow__node');
      reactFlowNodes.forEach((node) => {
        node.classList.add('canvas-node', 'generic-node');
      });

      // Add .canvas-edge class to React Flow edges for E2E tests
      const reactFlowEdges = document.querySelectorAll('.react-flow__edge path');
      reactFlowEdges.forEach((edge) => {
        edge.classList.add('canvas-edge', 'edge');
      });
    };

    // Apply classes initially and on node/edge changes
    addCompatibilityClasses();

    // Re-apply when nodes or edges change
    const timer = setInterval(addCompatibilityClasses, 1000);

    return () => clearInterval(timer);
  }, [nodes, edges]);

  // Handle drag and drop from agent shelf
  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setIsDragActive(false); // End drag state

      // Handle canvas overlay drops by calculating position relative to React Flow
      let position;
      const isCanvasTarget = (event.target as Element)?.tagName === 'CANVAS';

      if (isCanvasTarget) {
        // E2E test dropping to canvas overlay - calculate React Flow relative position
        const reactFlowElement = document.querySelector('.react-flow');
        if (reactFlowElement) {
          const reactFlowBounds = reactFlowElement.getBoundingClientRect();
          position = {
            x: event.clientX - reactFlowBounds.left,
            y: event.clientY - reactFlowBounds.top,
          };
        } else {
          return; // Can't find React Flow element
        }
      } else {
        // Normal React Flow drop
        const reactFlowBounds = (event.target as Element)?.closest('.react-flow')?.getBoundingClientRect();
        if (!reactFlowBounds) return;
        position = {
          x: event.clientX - reactFlowBounds.left,
          y: event.clientY - reactFlowBounds.top,
        };
      }

      const agentId = event.dataTransfer.getData('agent-id');
      const agentName = event.dataTransfer.getData('agent-name');
      const toolType = event.dataTransfer.getData('tool-type');
      const toolName = event.dataTransfer.getData('tool-name');

      if (agentId && agentName) {
        const newNode: Node = {
          id: `agent-${Date.now()}`,
          type: 'agent',
          position,
          data: {
            label: agentName,
            agentId: parseInt(agentId, 10),
          },
        };
        setNodes((nds: Node[]) => [...nds, newNode]);
      } else if (toolType && toolName) {
        const newNode: Node = {
          id: `tool-${Date.now()}`,
          type: 'tool',
          position,
          data: {
            label: toolName,
            toolType,
          },
        };
        setNodes((nds: Node[]) => [...nds, newNode]);
      }
    },
    [setNodes]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  // Global drag end handler to reset drag state
  useEffect(() => {
    const handleDragEnd = () => setIsDragActive(false);
    const handleDrop = () => setIsDragActive(false);

    document.addEventListener('dragend', handleDragEnd);
    document.addEventListener('drop', handleDrop);

    return () => {
      document.removeEventListener('dragend', handleDragEnd);
      document.removeEventListener('drop', handleDrop);
    };
  }, []);

  return (
    <>
      <div
        id="agent-shelf"
        data-testid="agent-shelf"
        className="agent-shelf"
      >
          <h3>Available Agents</h3>
          <div className="agent-shelf-content">
            {agents.length === 0 ? (
              <p>No agents available</p>
            ) : (
              agents.map((agent) => (
                <div
                  key={agent.id}
                  className="agent-shelf-item agent-pill"
                  data-testid={`shelf-agent-${agent.id}`}
                  draggable={true}
                  onDragStart={(e) => {
                    e.dataTransfer.setData('agent-id', String(agent.id));
                    e.dataTransfer.setData('agent-name', agent.name);
                    setIsDragActive(true);
                  }}
                >
                  <div className="agent-icon">🤖</div>
                  <div className="agent-name">{agent.name}</div>
                </div>
              ))
            )}
          </div>
        </div>

      <div
        id="canvas-container"
        data-testid="canvas-container"
        className="canvas-container"
      >
        <div className="main-content-area">
          {/* Execution Controls */}
          <div className="execution-controls">
            <div className="execution-buttons">
              <button
                className={`run-button ${executeWorkflowMutation.isPending ? 'loading' : ''}`}
                onClick={() => executeWorkflowMutation.mutate()}
                disabled={executeWorkflowMutation.isPending || !workflow?.id || (currentExecution?.phase === 'running')}
                title="Run Workflow"
              >
                {executeWorkflowMutation.isPending ? '⏳' : '▶️'} Run
              </button>

              {currentExecution?.phase === 'running' && (
                <button
                  className="cancel-button"
                  onClick={() => cancelExecutionMutation.mutate()}
                  disabled={cancelExecutionMutation.isPending}
                  title="Cancel Execution"
                >
                  ⏹️ Cancel
                </button>
              )}

              {currentExecution && (
                <button
                  className="logs-button"
                  onClick={() => setShowLogs(!showLogs)}
                  title="Toggle Execution Logs"
                >
                  📋 Logs {showLogs ? '▼' : '▶️'}
                </button>
              )}
            </div>

            {/* Execution Status */}
            {currentExecution && (
              <div className={`execution-status execution-status--${currentExecution.phase}`}>
                <span className="execution-phase">
                  {currentExecution.phase === 'waiting' && '⏳ Waiting'}
                  {currentExecution.phase === 'running' && '🔄 Running'}
                  {currentExecution.phase === 'finished' && '✅ Finished'}
                  {currentExecution.phase === 'cancelled' && '❌ Cancelled'}
                </span>
                <span className="execution-id">ID: {currentExecution.execution_id}</span>
              </div>
            )}
          </div>

          <div className="canvas-workspace" data-testid="canvas-workspace">
            <div style={{ width: '100%', height: '100%', minHeight: '600px', position: 'relative' }}>
              {/* Canvas overlay for E2E test compatibility - only active during drag operations */}
              {isDragActive && (
                <canvas
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    height: '100%',
                    pointerEvents: 'auto',
                    opacity: 0,
                    zIndex: 100
                  }}
                  onDrop={onDrop}
                  onDragOver={onDragOver}
                />
              )}
              <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeDragStart={onNodeDragStart}
                onNodeDragStop={onNodeDragStop}
                onDrop={onDrop}
                onDragOver={onDragOver}
                nodeTypes={nodeTypes}
                fitView
              >
                <Background />
                <Controls />
                <MiniMap />
              </ReactFlow>
            </div>
          </div>
        </div>
      </div>

      <div
        id="tool-palette"
        data-testid="tool-palette"
        className="tool-palette"
      >
        <h3>Tools</h3>
        <div className="tool-palette-content">
          <div
            className="tool-palette-item"
            data-testid="tool-http-request"
            draggable={true}
            onDragStart={(e) => {
              e.dataTransfer.setData('tool-type', 'http-request');
              e.dataTransfer.setData('tool-name', 'HTTP Request');
              setIsDragActive(true);
            }}
          >
            <div className="tool-icon">🌐</div>
            <div className="tool-name">HTTP Request</div>
          </div>
          <div
            className="tool-palette-item"
            data-testid="tool-url-fetch"
            draggable={true}
            onDragStart={(e) => {
              e.dataTransfer.setData('tool-type', 'url-fetch');
              e.dataTransfer.setData('tool-name', 'URL Fetch');
              setIsDragActive(true);
            }}
          >
            <div className="tool-icon">📡</div>
            <div className="tool-name">URL Fetch</div>
          </div>
        </div>
      </div>

      {/* Execution Logs Panel */}
      {showLogs && currentExecution && (
        <div className="execution-logs-panel">
          <div className="logs-header">
            <h4>Execution Logs</h4>
            <button
              className="close-logs"
              onClick={() => setShowLogs(false)}
              title="Close Logs"
            >
              ✕
            </button>
          </div>
          <div className="logs-content">
            <div className="execution-info">
              <div>Execution ID: {currentExecution.execution_id}</div>
              <div>Status: {currentExecution.phase}</div>
              {currentExecution.result !== undefined && currentExecution.result !== null && (
                <div>
                  Result: <pre>{String(JSON.stringify(currentExecution.result, null, 2) || 'null')}</pre>
                </div>
              )}
            </div>
            <div className="logs-output">
              <h5>Logs:</h5>
              <pre className="logs-text">
                {executionLogs || "No logs available yet..."}
              </pre>
            </div>
          </div>
        </div>
      )}

      {(isLoading || saveWorkflowMutation.isPending) && (
        <div className="saving-indicator">
          Saving workflow...
        </div>
      )}
    </>
  );
}

// Simple debounce utility function
function debounce<T extends unknown[]>(
  func: (...args: T) => void,
  wait: number
): (...args: T) => void {
  let timeout: number | undefined;
  return (...args: T) => {
    clearTimeout(timeout);
    timeout = window.setTimeout(() => func(...args), wait);
  };
}
