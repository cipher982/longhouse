import React, { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "../lib/useWebSocket";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  addEdge,
  ViewportPortal,
  useNodesState,
  useEdgesState,
  useReactFlow,
  useStore,
  type Node as FlowNode,
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

type ToolPaletteItem = {
  type: string;
  name: string;
  icon: string;
};

type ShelfSection = "agents" | "tools";

const TOOL_ITEMS: ToolPaletteItem[] = [
  { type: "http-request", name: "HTTP Request", icon: "🌐" },
  { type: "url-fetch", name: "URL Fetch", icon: "📡" },
] ;

const SECTION_STATE_STORAGE_KEY = "canvas_section_state";
const DEFAULT_SECTION_STATE: Record<ShelfSection, boolean> = {
  agents: false,
  tools: false,
};
const SNAP_GRID_SIZE = 24;

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

const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max);

// Convert backend WorkflowData to React Flow format
function convertToReactFlowData(workflowData: WorkflowData): { nodes: FlowNode[]; edges: Edge[] } {
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
function normalizeWorkflow(nodes: FlowNode[], edges: Edge[]): WorkflowDataInput {
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

function CanvasPageContent() {
  const queryClient = useQueryClient();
  const reactFlowInstance = useReactFlow();
  const zoom = useStore((state) => state.transform[2]);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const lastSavedHashRef = useRef<string>("");
  const pendingHashesRef = useRef<Set<string>>(new Set());
  const currentExecutionRef = useRef<ExecutionStatus | null>(null);
  const canvasInitializedRef = useRef<boolean>(false);
  const initialFitDoneRef = useRef<boolean>(false);
  const toastIdRef = useRef<string | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);

  // Execution state
  const [currentExecution, setCurrentExecution] = useState<ExecutionStatus | null>(null);
  const [executionLogs, setExecutionLogs] = useState<string>("");
  const [isDragActive, setIsDragActive] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [showLogs, setShowLogs] = useState(false);
  const [dragPreviewData, setDragPreviewData] = useState<DragPreviewData | null>(null);
  const [dragPreviewPosition, setDragPreviewPosition] = useState<{ x: number; y: number } | null>(
    null
  );
  const transparentDragImage = React.useMemo(() => {
    const img = new Image();
    img.src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
    return img;
  }, []);
  const resetDragPreview = useCallback(() => {
    setDragPreviewData(null);
    setDragPreviewPosition(null);
  }, []);

  const [searchTerm, setSearchTerm] = useState("");
  const [collapsedSections, setCollapsedSections] = useState<Record<ShelfSection, boolean>>(() => {
    if (typeof window === "undefined") {
      return { ...DEFAULT_SECTION_STATE };
    }
    try {
      const stored = window.localStorage.getItem(SECTION_STATE_STORAGE_KEY);
      if (!stored) {
        return { ...DEFAULT_SECTION_STATE };
      }
      const parsed = JSON.parse(stored) as Partial<Record<ShelfSection, boolean>>;
      return { ...DEFAULT_SECTION_STATE, ...parsed };
    } catch (error) {
      console.warn("Failed to parse shelf section state:", error);
      return { ...DEFAULT_SECTION_STATE };
    }
  });
  const [snapToGridEnabled, setSnapToGridEnabled] = useState(true);
  const [guidesVisible, setGuidesVisible] = useState(true);
  const [contextMenu, setContextMenu] = useState<{ nodeId: string; x: number; y: number } | null>(null);
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);

  React.useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(SECTION_STATE_STORAGE_KEY, JSON.stringify(collapsedSections));
    } catch (error) {
      console.warn("Failed to persist shelf section state:", error);
    }
  }, [collapsedSections]);

  const toggleSection = useCallback((section: ShelfSection) => {
    setCollapsedSections((prev) => ({
      ...prev,
      [section]: !prev[section],
    }));
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const isFormField =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        target?.isContentEditable;

      if (isFormField) {
        return;
      }

      if (event.shiftKey) {
        const key = event.key.toLowerCase();
        if (key === "s") {
          event.preventDefault();
          setSnapToGridEnabled((prev) => !prev);
          return;
        }
        if (key === "g") {
          event.preventDefault();
          setGuidesVisible((prev) => !prev);
          return;
        }
        if (event.code === "Slash") {
          event.preventDefault();
          setShowShortcutHelp((prev) => !prev);
          return;
        }
      }

      if (event.key === "Escape" && showShortcutHelp) {
        event.preventDefault();
        setShowShortcutHelp(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [showShortcutHelp]);

  useEffect(() => {
    if (!contextMenu) {
      return;
    }

    const handlePointer = (event: MouseEvent) => {
      if (contextMenuRef.current?.contains(event.target as Node)) {
        return;
      }
      setContextMenu(null);
    };

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setContextMenu(null);
      }
    };

    window.addEventListener("mousedown", handlePointer);
    window.addEventListener("contextmenu", handlePointer);
    window.addEventListener("keydown", handleEscape);

    return () => {
      window.removeEventListener("mousedown", handlePointer);
      window.removeEventListener("contextmenu", handlePointer);
      window.removeEventListener("keydown", handleEscape);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (contextMenu && contextMenuRef.current) {
      contextMenuRef.current.focus();
    }
  }, [contextMenu]);

  const handleNodeContextMenu = useCallback((event: React.MouseEvent, node: FlowNode) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({
      nodeId: node.id,
      x: event.clientX,
      y: event.clientY,
    });
  }, []);

  const handleDuplicateNode = useCallback(() => {
    if (!contextMenu) return;
    const { nodeId } = contextMenu;
    setNodes((currentNodes) => {
      const sourceNode = currentNodes.find((node) => node.id === nodeId);
      if (!sourceNode) {
        return currentNodes;
      }
      const duplicatedNode: FlowNode = {
        ...sourceNode,
        id: `${sourceNode.id}-copy-${Date.now()}`,
        position: {
          x: sourceNode.position.x + SNAP_GRID_SIZE,
          y: sourceNode.position.y + SNAP_GRID_SIZE,
        },
        selected: false,
      };
      return [...currentNodes, duplicatedNode];
    });
    setContextMenu(null);
  }, [contextMenu, setNodes]);

  const handleDeleteNode = useCallback(() => {
    if (!contextMenu) return;
    const { nodeId } = contextMenu;
    setNodes((currentNodes) => currentNodes.filter((node) => node.id !== nodeId));
    setEdges((currentEdges) =>
      currentEdges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId)
    );
    setContextMenu(null);
  }, [contextMenu, setEdges, setNodes]);

  const handlePaneClick = useCallback(() => {
    setContextMenu(null);
  }, []);

  type DraggableAgent = { id: number; name: string };
  type DraggableTool = { type: string; name: string };

  type DragPreviewKind = "agent" | "tool";

  interface DragPreviewData {
    kind: DragPreviewKind;
    label: string;
    icon: string;
    baseSize: { width: number; height: number };
    pointerRatio: { x: number; y: number };
    agentId?: number;
    toolType?: string;
  }

  const resolveToolIcon = useCallback((toolType: string) => {
    return TOOL_ITEMS.find((tool) => tool.type === toolType)?.icon ?? "🔧";
  }, []);

  const beginAgentDrag = useCallback(
    (event: React.DragEvent, agent: DraggableAgent) => {
      event.dataTransfer.setData("agent-id", String(agent.id));
      event.dataTransfer.setData("agent-name", agent.name);
      event.dataTransfer.effectAllowed = "move";
      if (event.dataTransfer.setDragImage) {
        event.dataTransfer.setDragImage(transparentDragImage, 0, 0);
      }
      if (event.currentTarget instanceof HTMLElement) {
        event.currentTarget.setAttribute("aria-grabbed", "true");
        const rect = event.currentTarget.getBoundingClientRect();
        const clientX = event.clientX ?? 0;
        const clientY = event.clientY ?? 0;
        const pointerOffsetX = clientX - rect.left;
        const pointerOffsetY = clientY - rect.top;
        const pointerRatioX = rect.width ? clamp(pointerOffsetX / rect.width, 0, 1) : 0;
        const pointerRatioY = rect.height ? clamp(pointerOffsetY / rect.height, 0, 1) : 0;
        setDragPreviewData({
          kind: "agent",
          label: agent.name,
          icon: "🤖",
          baseSize: { width: rect.width || 160, height: rect.height || 48 },
          pointerRatio: { x: pointerRatioX, y: pointerRatioY },
          agentId: agent.id,
        });
        if (clientX !== 0 || clientY !== 0) {
          const baseWidth = rect.width || 160;
          const baseHeight = rect.height || 48;
          const offsetX = baseWidth * zoom * pointerRatioX;
          const offsetY = baseHeight * zoom * pointerRatioY;
          const initialPosition = reactFlowInstance.screenToFlowPosition({
            x: clientX - offsetX,
            y: clientY - offsetY,
          });
          setDragPreviewPosition(initialPosition);
        } else {
          setDragPreviewPosition(null);
        }
      } else {
        setDragPreviewData({
          kind: "agent",
          label: agent.name,
          icon: "🤖",
          baseSize: { width: 160, height: 48 },
          pointerRatio: { x: 0, y: 0 },
          agentId: agent.id,
        });
        setDragPreviewPosition(null);
      }
      setIsDragActive(true);
    },
    [reactFlowInstance, setIsDragActive, transparentDragImage, zoom]
  );

  const beginToolDrag = useCallback(
    (event: React.DragEvent, tool: DraggableTool) => {
      event.dataTransfer.setData("tool-type", tool.type);
      event.dataTransfer.setData("tool-name", tool.name);
      event.dataTransfer.effectAllowed = "move";
      if (event.dataTransfer.setDragImage) {
        event.dataTransfer.setDragImage(transparentDragImage, 0, 0);
      }
      if (event.currentTarget instanceof HTMLElement) {
        event.currentTarget.setAttribute("aria-grabbed", "true");
        const rect = event.currentTarget.getBoundingClientRect();
        const clientX = event.clientX ?? 0;
        const clientY = event.clientY ?? 0;
        const pointerOffsetX = clientX - rect.left;
        const pointerOffsetY = clientY - rect.top;
        const pointerRatioX = rect.width ? clamp(pointerOffsetX / rect.width, 0, 1) : 0;
        const pointerRatioY = rect.height ? clamp(pointerOffsetY / rect.height, 0, 1) : 0;
        setDragPreviewData({
          kind: "tool",
          label: tool.name,
          icon: resolveToolIcon(tool.type),
          baseSize: { width: rect.width || 160, height: rect.height || 48 },
          pointerRatio: { x: pointerRatioX, y: pointerRatioY },
          toolType: tool.type,
        });
        if (clientX !== 0 || clientY !== 0) {
          const baseWidth = rect.width || 160;
          const baseHeight = rect.height || 48;
          const offsetX = baseWidth * zoom * pointerRatioX;
          const offsetY = baseHeight * zoom * pointerRatioY;
          const initialPosition = reactFlowInstance.screenToFlowPosition({
            x: clientX - offsetX,
            y: clientY - offsetY,
          });
          setDragPreviewPosition(initialPosition);
        } else {
          setDragPreviewPosition(null);
        }
      } else {
        setDragPreviewData({
          kind: "tool",
          label: tool.name,
          icon: resolveToolIcon(tool.type),
          baseSize: { width: 160, height: 48 },
          pointerRatio: { x: 0, y: 0 },
          toolType: tool.type,
        });
        setDragPreviewPosition(null);
      }
      setIsDragActive(true);
    },
    [reactFlowInstance, resolveToolIcon, setIsDragActive, transparentDragImage, zoom]
  );

  useEffect(() => {
    if (!dragPreviewData) {
      return;
    }

    const handleDragOver = (event: DragEvent) => {
      if (!dragPreviewData) {
        return;
      }
      event.preventDefault();
      if (event.clientX === 0 && event.clientY === 0) {
        return;
      }
      const baseWidth = dragPreviewData.baseSize.width || 1;
      const baseHeight = dragPreviewData.baseSize.height || 1;
      const offsetX = baseWidth * zoom * dragPreviewData.pointerRatio.x;
      const offsetY = baseHeight * zoom * dragPreviewData.pointerRatio.y;
      const flowPosition = reactFlowInstance.screenToFlowPosition({
        x: event.clientX - offsetX,
        y: event.clientY - offsetY,
      });
      setDragPreviewPosition(flowPosition);
    };

    const handleDragEnd = () => {
      resetDragPreview();
    };

    document.addEventListener("dragover", handleDragOver);
    document.addEventListener("dragend", handleDragEnd);
    document.addEventListener("drop", handleDragEnd);

    return () => {
      document.removeEventListener("dragover", handleDragOver);
      document.removeEventListener("dragend", handleDragEnd);
      document.removeEventListener("drop", handleDragEnd);
    };
  }, [dragPreviewData, reactFlowInstance, resetDragPreview, zoom]);

  // Fetch agents for the shelf
  const { data: agents = [] } = useQuery<AgentSummary[]>({
    queryKey: ["agents", { scope: "my" }],
    queryFn: () => fetchAgents({ scope: "my" }),
    refetchInterval: 2000, // Poll every 2 seconds
  });

  const filteredAgents = React.useMemo(() => {
    const normalized = searchTerm.trim().toLowerCase();
    if (!normalized) {
      return agents;
    }
    return agents.filter((agent) => agent.name.toLowerCase().includes(normalized));
  }, [agents, searchTerm]);

  const filteredTools = React.useMemo(() => {
    const normalized = searchTerm.trim().toLowerCase();
    if (!normalized) {
      return TOOL_ITEMS;
    }
    return TOOL_ITEMS.filter((tool) => tool.name.toLowerCase().includes(normalized));
  }, [searchTerm]);

  // Fetch current workflow
  const { data: workflow } = useQuery<Workflow>({
    queryKey: ["workflow", "current"],
    queryFn: fetchCurrentWorkflow,
    staleTime: 30000, // Consider data fresh for 30 seconds
  });

  // Initialize nodes and edges from workflow data ONLY on first load
  // This prevents flickering when server state updates after user drags nodes
  React.useEffect(() => {
    if (workflow?.canvas && !canvasInitializedRef.current) {
      const { nodes: flowNodes, edges: flowEdges } = convertToReactFlowData(workflow.canvas);
      setNodes(flowNodes);
      setEdges(flowEdges);
      canvasInitializedRef.current = true;

      // Initialize hash from loaded workflow
      const normalized = normalizeWorkflow(flowNodes, flowEdges);
      hashWorkflow(normalized).then((hash) => {
        lastSavedHashRef.current = hash;
      });
    }
  }, [workflow, setNodes, setEdges]);

  useEffect(() => {
    if (!canvasInitializedRef.current || initialFitDoneRef.current) {
      return;
    }

    if (nodes.length === 0) {
      initialFitDoneRef.current = true;
      return;
    }

    const frame = requestAnimationFrame(() => {
      try {
        reactFlowInstance.fitView({ maxZoom: 1, duration: 200 });
      } catch (error) {
        console.warn("Failed to fit initial view:", error);
      }
    });

    initialFitDoneRef.current = true;

    return () => cancelAnimationFrame(frame);
  }, [nodes, reactFlowInstance]);

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
  const debouncedSave = React.useMemo(() => {
    return debounce((nodes: FlowNode[], edges: Edge[]) => {
      if (nodes.length > 0 || edges.length > 0) {
        const workflowData = normalizeWorkflow(nodes, edges);
        saveWorkflowMutation.mutate(workflowData);
      }
    }, 1000);
  }, [saveWorkflowMutation]);

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

  const isSaving = saveWorkflowMutation.isPending;

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
  }, []);

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

      // Use ReactFlow's screenToFlowPosition to properly convert screen coordinates
      // to flow coordinates, accounting for zoom/pan transforms
      const pointerAdjustment = dragPreviewData
        ? {
            x:
              (dragPreviewData.baseSize.width || 0) *
              zoom *
              dragPreviewData.pointerRatio.x,
            y:
              (dragPreviewData.baseSize.height || 0) *
              zoom *
              dragPreviewData.pointerRatio.y,
          }
        : { x: 0, y: 0 };
      const position = reactFlowInstance.screenToFlowPosition({
        x: event.clientX - pointerAdjustment.x,
        y: event.clientY - pointerAdjustment.y,
      });

      const agentId = event.dataTransfer.getData('agent-id');
      const agentName = event.dataTransfer.getData('agent-name');
      const toolType = event.dataTransfer.getData('tool-type');
      const toolName = event.dataTransfer.getData('tool-name');

      if (agentId && agentName) {
        const parsedAgentId = parseInt(agentId, 10);
        if (Number.isNaN(parsedAgentId)) {
          return;
        }
        const newNode: FlowNode = {
          id: `agent-${Date.now()}`,
          type: 'agent',
          position,
          data: {
            label: agentName,
            agentId: parsedAgentId,
          },
        };
        setNodes((nds: FlowNode[]) => [...nds, newNode]);
      } else if (toolType && toolName) {
        const toolIcon = resolveToolIcon(toolType);
        const newNode: FlowNode = {
          id: `tool-${Date.now()}`,
          type: 'tool',
          position,
          data: {
            label: toolName,
            toolType,
          },
        };
        setNodes((nds: FlowNode[]) => [...nds, newNode]);
      }
      resetDragPreview();
    },
    [dragPreviewData, reactFlowInstance, resolveToolIcon, resetDragPreview, setNodes, zoom]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  // Global drag end handler to reset drag state
  useEffect(() => {
    const resetGrabState = () => {
      document.querySelectorAll('[aria-grabbed="true"]').forEach((element) => {
        (element as HTMLElement).setAttribute('aria-grabbed', 'false');
      });
    };

    const handleDragEnd = () => {
      setIsDragActive(false);
      resetDragPreview();
      resetGrabState();
    };
    const handleDrop = () => {
      setIsDragActive(false);
      resetDragPreview();
      resetGrabState();
    };

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
        <section className="agent-shelf-section shelf-search">
          <label htmlFor="canvas-shelf-search" className="shelf-search-label">
            Search
          </label>
          <input
            id="canvas-shelf-search"
            type="search"
            className="shelf-search-input"
            placeholder="Filter agents or tools"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
          />
        </section>

        <section className="agent-shelf-section">
          <button
            type="button"
            className="shelf-section-toggle"
            onClick={() => toggleSection("agents")}
            aria-expanded={!collapsedSections.agents}
            aria-controls="shelf-agent-list"
          >
            <span className="caret">{collapsedSections.agents ? "▸" : "▾"}</span>
            <span>Agents</span>
            <span className="count">{filteredAgents.length}</span>
          </button>
          {!collapsedSections.agents &&
            (filteredAgents.length > 0 ? (
              <div id="shelf-agent-list" className="agent-shelf-content">
                {filteredAgents.map((agent) => (
                  <div
                    key={agent.id}
                    className="agent-shelf-item agent-pill"
                    data-testid={`shelf-agent-${agent.id}`}
                    draggable={true}
                    role="button"
                    tabIndex={0}
                    aria-grabbed="false"
                    aria-label={`Drag agent ${agent.name} onto the canvas`}
                    onDragStart={(event) => beginAgentDrag(event, { id: agent.id, name: agent.name })}
                    onDragEnd={(event) => {
                      if (event.currentTarget instanceof HTMLElement) {
                        event.currentTarget.setAttribute('aria-grabbed', 'false');
                      }
                    }}
                  >
                    <div className="agent-icon">🤖</div>
                    <div className="agent-name">{agent.name}</div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="shelf-empty">
                {searchTerm ? `No agents found for "${searchTerm}".` : "No agents available."}
              </p>
            ))}
        </section>

        <section
          id="tool-palette"
          data-testid="tool-palette"
          className="agent-shelf-section"
        >
          <button
            type="button"
            className="shelf-section-toggle"
            onClick={() => toggleSection("tools")}
            aria-expanded={!collapsedSections.tools}
            aria-controls="shelf-tool-list"
          >
            <span className="caret">{collapsedSections.tools ? "▸" : "▾"}</span>
            <span>Tools</span>
            <span className="count">{filteredTools.length}</span>
          </button>
          {!collapsedSections.tools &&
            (filteredTools.length > 0 ? (
              <div id="shelf-tool-list" className="tool-palette-content">
                {filteredTools.map((tool) => (
                  <div
                    key={tool.type}
                    className="tool-palette-item"
                    data-testid={`tool-${tool.type}`}
                    draggable={true}
                    role="button"
                    tabIndex={0}
                    aria-grabbed="false"
                    aria-label={`Drag tool ${tool.name} onto the canvas`}
                    onDragStart={(event) => beginToolDrag(event, tool)}
                    onDragEnd={(event) => {
                      if (event.currentTarget instanceof HTMLElement) {
                        event.currentTarget.setAttribute('aria-grabbed', 'false');
                      }
                    }}
                  >
                    <div className="tool-icon">{tool.icon}</div>
                    <div className="tool-name">{tool.name}</div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="shelf-empty">
                {searchTerm ? `No tools found for "${searchTerm}".` : "No tools available."}
              </p>
            ))}
        </section>
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
                  aria-expanded={showLogs}
                  aria-controls="execution-logs-drawer"
                >
                  📋 Logs {showLogs ? '▼' : '▶️'}
                </button>
              )}

              <div className="canvas-mode-toggles" role="group" aria-label="Canvas display toggles">
                <button
                  type="button"
                  className="canvas-toggle-btn"
                  onClick={() => setSnapToGridEnabled((prev) => !prev)}
                  aria-pressed={snapToGridEnabled}
                  aria-label={snapToGridEnabled ? 'Disable snap to grid (Shift+S)' : 'Enable snap to grid (Shift+S)'}
                  title={`Snap to grid ${snapToGridEnabled ? 'enabled' : 'disabled'} (Shift+S)`}
                >
                  ⬛
                </button>
                <button
                  type="button"
                  className="canvas-toggle-btn"
                  onClick={() => setGuidesVisible((prev) => !prev)}
                  aria-pressed={guidesVisible}
                  aria-label={guidesVisible ? 'Hide guides (Shift+G)' : 'Show guides (Shift+G)'}
                  title={`Guides ${guidesVisible ? 'visible' : 'hidden'} (Shift+G)`}
                >
                  #️⃣
                </button>
              </div>
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

          <div
            className={`canvas-workspace${showLogs && currentExecution ? ' logs-open' : ''}`}
            data-testid="canvas-workspace"
          >
            <div className="canvas-stage">
              {isSaving && (
                <div className="canvas-save-banner" role="status" aria-live="polite">
                  {saveWorkflowMutation.isPending ? 'Saving changes...' : 'Syncing workflow...'}
                </div>
              )}
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
                snapToGrid={snapToGridEnabled}
                snapGrid={[SNAP_GRID_SIZE, SNAP_GRID_SIZE]}
                selectionOnDrag
                panOnScroll
                multiSelectionKeyCode="Shift"
                onPaneClick={handlePaneClick}
                onNodeContextMenu={handleNodeContextMenu}
              >
                {dragPreviewData && dragPreviewPosition && (
                  <ViewportPortal>
                    <div
                      className="canvas-drag-preview"
                      style={{
                        position: "absolute",
                        transform: `translate(${dragPreviewPosition.x}px, ${dragPreviewPosition.y}px)`,
                        pointerEvents: "none",
                        width: `${dragPreviewData.baseSize.width || 160}px`,
                        height: `${dragPreviewData.baseSize.height || 48}px`,
                      }}
                    >
                      {dragPreviewData.kind === "agent" ? (
                        <div className="agent-node drag-preview-node">
                          <div className="agent-icon">{dragPreviewData.icon}</div>
                          <div className="agent-name">{dragPreviewData.label}</div>
                        </div>
                      ) : (
                        <div className="tool-node drag-preview-node">
                          <div className="tool-icon">{dragPreviewData.icon}</div>
                          <div className="tool-name">{dragPreviewData.label}</div>
                        </div>
                      )}
                    </div>
                  </ViewportPortal>
                )}
                {guidesVisible && <Background gap={SNAP_GRID_SIZE} />}
                <Controls />
                <MiniMap />
              </ReactFlow>
            </div>
            {showLogs && currentExecution && (
              <aside
                id="execution-logs-drawer"
                className="execution-logs-drawer"
                role="complementary"
                aria-label="Execution logs"
              >
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
              </aside>
            )}
          </div>
        </div>
      </div>

      {showShortcutHelp && (
        <div className="shortcut-help-overlay" role="dialog" aria-modal="true" aria-labelledby="shortcut-help-title">
          <div className="shortcut-help-panel">
            <div className="shortcut-help-header">
              <h3 id="shortcut-help-title">Canvas Shortcuts</h3>
              <button
                type="button"
                className="close-logs"
                onClick={() => setShowShortcutHelp(false)}
                title="Close shortcuts"
              >
                ✕
              </button>
            </div>
            <ul className="shortcut-help-list">
              <li><kbd>Shift</kbd> + <kbd>S</kbd> Toggle snap to grid</li>
              <li><kbd>Shift</kbd> + <kbd>G</kbd> Toggle guides</li>
              <li><kbd>Shift</kbd> + <kbd>/</kbd> Show this panel</li>
            </ul>
            <p className="shortcut-help-hint">Press Esc to close.</p>
          </div>
        </div>
      )}

      {contextMenu && (
        <div
          ref={contextMenuRef}
          className="canvas-context-menu"
          role="menu"
          tabIndex={-1}
          style={{ top: contextMenu.y, left: contextMenu.x }}
        >
          <button type="button" role="menuitem" onClick={handleDuplicateNode}>
            Duplicate node
          </button>
          <button type="button" role="menuitem" onClick={handleDeleteNode}>
            Delete node
          </button>
        </div>
      )}

    </>
  );
}

// Wrapper component that provides ReactFlow context
export default function CanvasPage() {
  return (
    <ReactFlowProvider>
      <CanvasPageContent />
    </ReactFlowProvider>
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
