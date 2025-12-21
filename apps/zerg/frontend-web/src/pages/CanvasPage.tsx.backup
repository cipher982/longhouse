import React, { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useShelf } from "../lib/useShelfState";
import { useWebSocket } from "../lib/useWebSocket";
import { usePointerDrag } from "../hooks/usePointerDrag";
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
import { ExecutionLogStream, type LogEntry } from "../components/ExecutionLogStream";
import { AgentIcon, GlobeIcon, SignalIcon, WrenchIcon, ZapIcon } from "../components/icons";
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
      <div className="agent-icon"><AgentIcon width={20} height={20} /></div>
      <div className="agent-name">{data.label}</div>
    </div>
  );
}

// Custom node component for tools
function ToolNode({ data }: { data: { label: string; toolType?: string } }) {
  const IconComponent = data.toolType === 'http-request' ? GlobeIcon : data.toolType === 'url-fetch' ? SignalIcon : WrenchIcon;

  return (
    <div className="tool-node">
      <div className="tool-icon"><IconComponent width={20} height={20} /></div>
      <div className="tool-name">{data.label}</div>
    </div>
  );
}

// Custom node component for triggers
function TriggerNode({ data }: { data: { label: string } }) {
  return (
    <div className="trigger-node">
      <div className="trigger-icon"><ZapIcon width={20} height={20} /></div>
      <div className="trigger-name">{data.label}</div>
    </div>
  );
}

// Custom node component for the MiniMap
// Uses foreignObject to render the actual node content (scaled down)
function MiniMapNode(props: any) {
  // Extract positioning and dimensions
  const { x, y, width, height, id } = props;

  // Retrieve the full node data from the store using the ID
  // properties like 'type' and 'data' are not passed directly to MiniMapNode in all versions
  const node = useStore((s) => s.nodeLookup.get(id));

  if (!node) return null;

  const { type, data } = node;

  return (
    <foreignObject x={x} y={y} width={width} height={height}>
      {/* We use a div with 100% size to contain the node component */}
      <div className="minimap-node-content" style={{ width: '100%', height: '100%' }}>
        {type === 'agent' && <AgentNode data={data as { label: string; agentId?: number }} />}
        {type === 'tool' && <ToolNode data={data as { label: string; toolType?: string }} />}
        {type === 'trigger' && <TriggerNode data={data as { label: string }} />}
      </div>
    </foreignObject>
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
  const { isShelfOpen } = useShelf();
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

  // Pointer/touch drag handler (cross-platform support)
  const { startDrag, updateDragPosition, endDrag, getDragData } = usePointerDrag();

  // Execution state
  const [currentExecution, setCurrentExecution] = useState<ExecutionStatus | null>(null);
  const [executionLogs, setExecutionLogs] = useState<LogEntry[]>([]);
  const [isDragActive, setIsDragActive] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [showLogs, setShowLogs] = useState(false);

  // Draggable logs panel state
  const [logsPanelPosition, setLogsPanelPosition] = useState<{ x: number; y: number } | null>(null);
  const [isDraggingLogsPanel, setIsDraggingLogsPanel] = useState(false);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const logsPanelRef = useRef<HTMLDivElement | null>(null);
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

  const updatePreviewPositionFromClientPoint = useCallback(
    (clientPoint: { x: number; y: number }, overridePreview?: DragPreviewData | null) => {
      const preview = overridePreview ?? dragPreviewData;
      if (!preview) {
        return;
      }

      if (!Number.isFinite(clientPoint.x) || !Number.isFinite(clientPoint.y)) {
        return;
      }

      const baseWidth = preview.baseSize.width || 1;
      const baseHeight = preview.baseSize.height || 1;
      const offsetX = baseWidth * zoom * preview.pointerRatio.x;
      const offsetY = baseHeight * zoom * preview.pointerRatio.y;
      const flowPosition = reactFlowInstance.screenToFlowPosition({
        x: clientPoint.x - offsetX,
        y: clientPoint.y - offsetY,
      });
      setDragPreviewPosition(flowPosition);
    },
    [dragPreviewData, reactFlowInstance, zoom]
  );

  type DropPayload =
    | { type: "agent"; agentId: number; label: string }
    | { type: "tool"; toolType: string; label: string };

  const toDropPayload = useCallback(
    (raw: { type: "agent" | "tool"; id?: string; name: string; tool_type?: string }): DropPayload | null => {
      if (!raw?.type || !raw.name) {
        return null;
      }

      if (raw.type === "agent") {
        const agentId = parseInt(raw.id ?? "", 10);
        if (!agentId) {
          return null;
        }
        return { type: "agent", agentId, label: raw.name };
      }

      if (raw.type === "tool") {
        if (typeof raw.tool_type !== "string" || raw.tool_type.length === 0) {
          return null;
        }
        return { type: "tool", toolType: raw.tool_type, label: raw.name };
      }

      return null;
    },
    []
  );

  const finalizeDrop = useCallback(
    (clientPoint: { x: number; y: number }, payload: DropPayload) => {
      const preview = dragPreviewData;
      const pointerAdjustment = preview
        ? {
            x: (preview.baseSize.width || 0) * zoom * preview.pointerRatio.x,
            y: (preview.baseSize.height || 0) * zoom * preview.pointerRatio.y,
          }
        : { x: 0, y: 0 };

      const position = reactFlowInstance.screenToFlowPosition({
        x: clientPoint.x - pointerAdjustment.x,
        y: clientPoint.y - pointerAdjustment.y,
      });

      const newNode: FlowNode =
        payload.type === "agent"
          ? {
              id: `agent-${Date.now()}`,
              type: "agent",
              position,
              data: {
                label: payload.label,
                agentId: payload.agentId,
              },
            }
          : {
              id: `tool-${Date.now()}`,
              type: "tool",
              position,
              data: {
                label: payload.label,
                toolType: payload.toolType,
              },
            };

      setNodes((nodes) => [...nodes, newNode]);
      setIsDragActive(false);
      resetDragPreview();
    },
    [dragPreviewData, reactFlowInstance, resetDragPreview, setNodes, zoom]
  );

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
      // Prevent event from bubbling to canvas (stops unwanted pan/zoom on mobile)
      event.stopPropagation();
      // NOTE: Do NOT call preventDefault() - it cancels HTML5 drag on desktop

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
        const preview: DragPreviewData = {
          kind: "agent",
          label: agent.name,
          icon: "🤖",
          baseSize: { width: rect.width || 160, height: rect.height || 48 },
          pointerRatio: { x: pointerRatioX, y: pointerRatioY },
          agentId: agent.id,
        };
        setDragPreviewData(preview);
        updatePreviewPositionFromClientPoint({ x: clientX, y: clientY }, preview);
      } else {
        const preview: DragPreviewData = {
          kind: "agent",
          label: agent.name,
          icon: "🤖",
          baseSize: { width: 160, height: 48 },
          pointerRatio: { x: 0, y: 0 },
          agentId: agent.id,
        };
        setDragPreviewData(preview);
        updatePreviewPositionFromClientPoint({ x: event.clientX ?? 0, y: event.clientY ?? 0 }, preview);
      }
      setIsDragActive(true);
    },
    [setIsDragActive, transparentDragImage, updatePreviewPositionFromClientPoint]
  );

  const beginToolDrag = useCallback(
    (event: React.DragEvent, tool: DraggableTool) => {
      // Prevent event from bubbling to canvas (stops unwanted pan/zoom on mobile)
      event.stopPropagation();
      // NOTE: Do NOT call preventDefault() - it cancels HTML5 drag on desktop

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
        const preview: DragPreviewData = {
          kind: "tool",
          label: tool.name,
          icon: resolveToolIcon(tool.type),
          baseSize: { width: rect.width || 160, height: rect.height || 48 },
          pointerRatio: { x: pointerRatioX, y: pointerRatioY },
          toolType: tool.type,
        };
        setDragPreviewData(preview);
        updatePreviewPositionFromClientPoint({ x: clientX, y: clientY }, preview);
      } else {
        const preview: DragPreviewData = {
          kind: "tool",
          label: tool.name,
          icon: resolveToolIcon(tool.type),
          baseSize: { width: 160, height: 48 },
          pointerRatio: { x: 0, y: 0 },
          toolType: tool.type,
        };
        setDragPreviewData(preview);
        updatePreviewPositionFromClientPoint({ x: event.clientX ?? 0, y: event.clientY ?? 0 }, preview);
      }
      setIsDragActive(true);
    },
    [resolveToolIcon, setIsDragActive, transparentDragImage, updatePreviewPositionFromClientPoint]
  );

  // Effect 1: HTML5 drag preview (desktop drag, depends on dragPreviewData)
  useEffect(() => {
    if (!dragPreviewData) {
      return;
    }

    const handleDragOver = (event: DragEvent) => {
      event.preventDefault();
      updatePreviewPositionFromClientPoint({ x: event.clientX, y: event.clientY });
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
  }, [dragPreviewData, resetDragPreview, updatePreviewPositionFromClientPoint]);

  // Effect 2: Pointer event handlers for touch/pen drag (independent of HTML5 preview)
  // CRITICAL: This must mount regardless of dragPreviewData state
  useEffect(() => {
    const handlePointerMove = (e: PointerEvent) => {
      updateDragPosition(e);
      updatePreviewPositionFromClientPoint({ x: e.clientX, y: e.clientY });
    };

    const handlePointerUp = (e: PointerEvent) => {
      const dragData = getDragData();

      if (!dragData) {
        endDrag(e);
        resetDragPreview();
        setIsDragActive(false);
        return;
      }

      const payload = toDropPayload(dragData);
      if (!payload) {
        endDrag(e);
        resetDragPreview();
        setIsDragActive(false);
        return;
      }

      updatePreviewPositionFromClientPoint({ x: e.clientX, y: e.clientY });
      finalizeDrop({ x: e.clientX, y: e.clientY }, payload);
      endDrag(e);
    };

    const handlePointerCancel = (e: PointerEvent) => {
      // Only handle if there's an active pointer drag
      const dragData = getDragData();
      if (!dragData) {
        return;
      }

      // Handle interrupted drags (e.g., user switches apps, browser gesture)
      endDrag(e);
      resetDragPreview();
      setIsDragActive(false);
    };

    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", handlePointerUp);
    document.addEventListener("pointercancel", handlePointerCancel);

    return () => {
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", handlePointerUp);
      document.removeEventListener("pointercancel", handlePointerCancel);
    };
  }, [
    finalizeDrop,
    getDragData,
    resetDragPreview,
    setIsDragActive,
    updateDragPosition,
    toDropPayload,
    endDrag,
    updatePreviewPositionFromClientPoint,
  ]);

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

  // Logs panel drag handlers
  const handleLogsPanelMouseDown = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    // Only initiate drag if clicking on the header, not the close button
    if ((event.target as HTMLElement).closest('.close-logs')) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    if (!logsPanelRef.current) return;

    const rect = logsPanelRef.current.getBoundingClientRect();
    setDragOffset({
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    });
    setIsDraggingLogsPanel(true);
  }, []);

  const handleLogsPanelMouseMove = useCallback((event: MouseEvent) => {
    if (!isDraggingLogsPanel || !logsPanelRef.current || !logsPanelPosition) return;

    const panel = logsPanelRef.current;
    const panelRect = panel.getBoundingClientRect();

    // Calculate new position
    let newX = event.clientX - dragOffset.x;
    let newY = event.clientY - dragOffset.y;

    // Get viewport bounds
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const panelWidth = panelRect.width;
    const panelHeight = panelRect.height;

    // Constrain to viewport bounds (leave at least 50px visible)
    const minVisible = 50;
    newX = Math.max(-panelWidth + minVisible, Math.min(newX, viewportWidth - minVisible));
    newY = Math.max(0, Math.min(newY, viewportHeight - minVisible));

    setLogsPanelPosition({ x: newX, y: newY });
  }, [isDraggingLogsPanel, dragOffset, logsPanelPosition]);

  const handleLogsPanelMouseUp = useCallback(() => {
    setIsDraggingLogsPanel(false);
  }, []);

  // Effect for logs panel dragging
  useEffect(() => {
    if (isDraggingLogsPanel) {
      document.addEventListener('mousemove', handleLogsPanelMouseMove);
      document.addEventListener('mouseup', handleLogsPanelMouseUp);
      return () => {
        document.removeEventListener('mousemove', handleLogsPanelMouseMove);
        document.removeEventListener('mouseup', handleLogsPanelMouseUp);
      };
    }
  }, [isDraggingLogsPanel, handleLogsPanelMouseMove, handleLogsPanelMouseUp]);

  // Initialize panel position when logs are opened
  useEffect(() => {
    if (showLogs && logsPanelPosition === null) {
      // Center the panel
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const panelWidth = 400; // Default width
      const panelHeight = 500; // Default max-height

      const newPosition = {
        x: (viewportWidth - panelWidth) / 2,
        y: Math.max(80, (viewportHeight - panelHeight) / 2),
      };

      console.log('[CanvasPage] 🪟 Initializing panel position:', newPosition);
      setLogsPanelPosition(newPosition);
    }
  }, [showLogs, logsPanelPosition]);

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
      console.log('[CanvasPage] 🚀 Starting workflow execution, workflow_id:', workflow.id);
      // Clear previous logs before starting
      setExecutionLogs([]);
      return startWorkflowExecution(workflow.id);
    },
    onSuccess: (execution) => {
      console.log('[CanvasPage] 🎯 Workflow started, execution_id:', execution.execution_id);
      setCurrentExecution(execution);
      toast.success("Workflow execution started! Watch the logs panel for real-time updates.");
      // Auto-open logs panel to show real-time stream
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
  const handleStreamingMessage = useCallback((envelope: any) => {
    // Use 'type' field from envelope (not 'message_type')
    const message_type = envelope.type || envelope.message_type;
    const data = envelope.data;

    // Only log non-token messages
    if (message_type !== 'stream_chunk') {
      console.log('[CanvasPage] 📨', message_type, 'for execution', data?.execution_id);
    }

    switch (message_type) {
      case 'execution_started': {
        console.log('[CanvasPage] ✅ Execution started:', data.execution_id);
        setExecutionLogs([{
          timestamp: Date.now(),
          type: 'execution',
          message: `EXECUTION STARTED [ID: ${data.execution_id}]`,
          metadata: data
        }]);
        break;
      }

      case 'node_state': {
        const { node_id, phase, result, output, error_message } = data;

        // Append to log stream
        const logType = error_message ? 'error' : (phase === 'running' ? 'node' : 'output');
        const logMessage = `NODE ${node_id} → ${phase.toUpperCase()}${result ? ` [${result}]` : ''}`;

        console.log('[CanvasPage] 📍 Node:', node_id, '→', phase, result || '');

        setExecutionLogs(prev => [...prev, {
          timestamp: Date.now(),
          type: logType,
          message: logMessage,
          metadata: data
        }]);

        // Update node visual state (future enhancement)
        // setNodes(currentNodes =>
        //   currentNodes.map(node =>
        //     node.id === node_id
        //       ? { ...node, data: { ...node.data, executionState: phase } }
        //       : node
        //   )
        // );
        break;
      }

      case 'workflow_progress': {
        const { completed_nodes } = data;
        // console.log('[CanvasPage] Workflow progress:', { completed: completed_nodes.length });
        // Optional: update progress indicator
        break;
      }

      case 'execution_finished': {
        const { result, error_message, duration_ms } = data;

        setExecutionLogs(prev => [...prev, {
          timestamp: Date.now(),
          type: 'execution',
          message: `EXECUTION ${result ? String(result).toUpperCase() : 'FINISHED'}${duration_ms ? ` (${duration_ms.toFixed(0)}ms)` : ''}${error_message ? ` - ${error_message}` : ''}`
        }]);

        console.log('[CanvasPage] 🏁 Execution finished:', result);

        // Refresh execution status via REST (to sync DB state)
        if (currentExecutionRef.current?.execution_id) {
          getExecutionStatus(currentExecutionRef.current.execution_id).then(status => {
            setCurrentExecution(status);
          }).catch(err => {
            console.error('[CanvasPage] Failed to fetch final execution status:', err);
          });
        }
        break;
      }

      default:
        // console.log('[CanvasPage] Unknown message type:', message_type);
        break;
    }
  }, []);

  const { sendMessage } = useWebSocket(currentExecution?.execution_id != null, {
    includeAuth: true,
    invalidateQueries: [],
    onStreamingMessage: handleStreamingMessage,
  });

  // Subscribe to workflow execution topic when execution starts
  useEffect(() => {
    if (!currentExecution?.execution_id) return;

    const topic = `workflow_execution:${currentExecution.execution_id}`;

    console.log('[CanvasPage] 📡 Subscribing to topic:', topic);

    // Subscribe to topic
    sendMessage({
      type: 'subscribe',
      topics: [topic]
    });

    // Cleanup: unsubscribe when execution changes or component unmounts
    return () => {
      console.log('[CanvasPage] 🔕 Unsubscribing from topic:', topic);
      sendMessage({
        type: 'unsubscribe',
        topics: [topic]
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentExecution?.execution_id]); // Only re-subscribe when execution ID changes

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

      const agentId = event.dataTransfer.getData("agent-id");
      const agentName = event.dataTransfer.getData("agent-name");
      const toolType = event.dataTransfer.getData("tool-type");
      const toolName = event.dataTransfer.getData("tool-name");

      let payload: DropPayload | null = null;

      if (agentId && agentName) {
        payload = toDropPayload({ type: "agent", id: agentId, name: agentName });
      } else if (toolType && toolName) {
        payload = toDropPayload({ type: "tool", name: toolName, tool_type: toolType });
      }

      if (!payload) {
        setIsDragActive(false);
        resetDragPreview();
        return;
      }

      updatePreviewPositionFromClientPoint({ x: event.clientX, y: event.clientY }, dragPreviewData);
      finalizeDrop({ x: event.clientX, y: event.clientY }, payload);
    },
    [dragPreviewData, finalizeDrop, resetDragPreview, setIsDragActive, toDropPayload, updatePreviewPositionFromClientPoint]
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
  }, [resetDragPreview]);

  return (
    <>
      <div
        id="agent-shelf"
        data-testid="agent-shelf"
        className={clsx("agent-shelf", { open: isShelfOpen })}
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
                    onPointerDown={(event) => {
                      // Only use Pointer API for touch/pen; let HTML5 drag handle mouse
                      if (event.isPrimary && event.pointerType !== 'mouse') {
                        // Start pointer drag tracking
                        startDrag(event as unknown as React.PointerEvent, {
                          type: 'agent',
                          id: agent.id.toString(),
                          name: agent.name
                        });

                        // Set drag preview data for visual feedback
                        const rect = event.currentTarget.getBoundingClientRect();
                        const pointerOffsetX = event.clientX - rect.left;
                        const pointerOffsetY = event.clientY - rect.top;
                        const preview: DragPreviewData = {
                          kind: 'agent',
                          label: agent.name,
                          icon: '🤖',
                          baseSize: { width: rect.width || 160, height: rect.height || 48 },
                          pointerRatio: {
                            x: rect.width ? pointerOffsetX / rect.width : 0,
                            y: rect.height ? pointerOffsetY / rect.height : 0
                          },
                          agentId: agent.id,
                        };
                        setDragPreviewData(preview);
                        updatePreviewPositionFromClientPoint({ x: event.clientX, y: event.clientY }, preview);
                        setIsDragActive(true);

                        event.currentTarget.setAttribute('aria-grabbed', 'true');
                      }
                    }}
                  >
                    {/* Icon added via CSS ::before pseudo-element */}
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
                    onPointerDown={(event) => {
                      // Only use Pointer API for touch/pen; let HTML5 drag handle mouse
                      if (event.isPrimary && event.pointerType !== 'mouse') {
                        // Start pointer drag tracking
                        startDrag(event as unknown as React.PointerEvent, {
                          type: 'tool',
                          name: tool.name,
                          tool_type: tool.type
                        });

                        // Set drag preview data for visual feedback
                        const rect = event.currentTarget.getBoundingClientRect();
                        const pointerOffsetX = event.clientX - rect.left;
                        const pointerOffsetY = event.clientY - rect.top;
                        const preview: DragPreviewData = {
                          kind: 'tool',
                          label: tool.name,
                          icon: tool.icon,
                          baseSize: { width: rect.width || 160, height: rect.height || 48 },
                          pointerRatio: {
                            x: rect.width ? pointerOffsetX / rect.width : 0,
                            y: rect.height ? pointerOffsetY / rect.height : 0
                          },
                          toolType: tool.type,
                        };
                        setDragPreviewData(preview);
                        updatePreviewPositionFromClientPoint({ x: event.clientX, y: event.clientY }, preview);
                        setIsDragActive(true);

                        event.currentTarget.setAttribute('aria-grabbed', 'true');
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
              {(() => {
                const hasNodes = nodes.length > 0;
                const isRunning = currentExecution?.phase === 'running';
                const isPending = executeWorkflowMutation.isPending;
                const noWorkflow = !workflow?.id;
                const isDisabled = isPending || noWorkflow || isRunning || !hasNodes;

                // Determine the appropriate tooltip
                let tooltip = "Run Workflow";
                if (isPending) tooltip = "Starting workflow...";
                else if (isRunning) tooltip = "Workflow is already running";
                else if (noWorkflow) tooltip = "No workflow loaded";
                else if (!hasNodes) tooltip = "Add nodes to the canvas before running";

                return (
                  <button
                    className={`run-button ${isPending ? 'loading' : ''}`}
                    onClick={() => executeWorkflowMutation.mutate()}
                    disabled={isDisabled}
                    title={tooltip}
                  >
                    {isPending ? '⏳' : '▶️'} Run
                  </button>
                );
              })()}

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
              <div
                className={`execution-status execution-status--${currentExecution.phase}`}
                onClick={() => setShowLogs(!showLogs)}
                style={{ cursor: 'pointer' }}
                title={showLogs ? "Click to hide execution details" : "Click to show execution details"}
              >
                <span className="execution-phase">
                  {currentExecution.phase === 'waiting' && '⏳ Waiting'}
                  {currentExecution.phase === 'running' && '🔄 Running'}
                  {currentExecution.phase === 'finished' && '✅ Finished'}
                  {currentExecution.phase === 'cancelled' && '❌ Cancelled'}
                </span>
                <span className="execution-id">ID: {currentExecution.execution_id}</span>
                <span className="execution-toggle-hint" style={{ fontSize: '0.8em', opacity: 0.7, marginLeft: '8px' }}>
                  {showLogs ? '▼' : '▶'}
                </span>
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
                <MiniMap
                  nodeComponent={MiniMapNode}
                  maskColor="rgba(20, 20, 35, 0.6)"
                  style={{
                    backgroundColor: '#2a2a3a', // Match card background
                    height: 120,
                    width: 160,
                    border: '1px solid #3d3d5c',
                    borderRadius: '4px'
                  }}
                />
              </ReactFlow>
            </div>
            {showLogs && currentExecution && (
              <aside
                ref={logsPanelRef}
                id="execution-logs-drawer"
                className={`execution-logs-draggable ${isDraggingLogsPanel ? 'dragging' : ''}`}
                role="complementary"
                aria-label="Execution logs"
                style={{
                  left: logsPanelPosition ? `${logsPanelPosition.x}px` : '50%',
                  top: logsPanelPosition ? `${logsPanelPosition.y}px` : '20%',
                  transform: logsPanelPosition ? 'none' : 'translateX(-50%)',
                }}
              >
                <div
                  className="logs-header"
                  onMouseDown={handleLogsPanelMouseDown}
                  style={{ cursor: isDraggingLogsPanel ? 'grabbing' : 'grab' }}
                >
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
                  <ExecutionLogStream
                    logs={executionLogs}
                    isRunning={currentExecution.phase === 'running'}
                  />
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

      {/* Scrim overlay (decorative, pointer-events: none to allow drag/drop) */}
      <div
        className={clsx("shelf-scrim", { "shelf-scrim--visible": isShelfOpen })}
      />
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
