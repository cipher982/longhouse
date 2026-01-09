import React, { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
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
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "../styles/canvas-react.css";
import toast from "react-hot-toast";
import type { LogEntry } from "../components/ExecutionLogStream";
import {
  fetchCurrentWorkflow,
  fetchWorkflowById,
  fetchWorkflowByName,
  updateWorkflowCanvas,
  startWorkflowExecution,
  getExecutionStatus,
  cancelExecution,
  type Workflow,
  type WorkflowDataInput,
  type ExecutionStatus,
} from "../services/api";
import { useWebSocket } from "../lib/useWebSocket";
import type { WebSocketMessage } from "../lib/useWebSocket";
import { AgentShelf } from "./canvas/AgentShelf";
import { ExecutionControls } from "./canvas/ExecutionControls";
import { ExecutionLogsPanel } from "./canvas/ExecutionLogsPanel";
import { nodeTypes, MiniMapNode } from "./canvas/NodeComponents";
import { convertToReactFlowData, normalizeWorkflow, hashWorkflow } from "./canvas/workflowUtils";
import {
  type DragPreviewData,
  type DropPayload,
  toDropPayload,
  clamp,
  createTransparentDragImage,
} from "./canvas/dragDropUtils";
import { SNAP_GRID_SIZE, debounce } from "./canvas/utils";

import { getNodeIcon } from "../lib/iconUtils";
import { XIcon } from "../components/icons";

function CanvasPageContent() {
  const queryClient = useQueryClient();
  const reactFlowInstance = useReactFlow();
  const [searchParams] = useSearchParams();
  const workflowIdParam = searchParams.get("workflow");
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

  const [dragPreviewData, setDragPreviewData] = useState<DragPreviewData | null>(null);
  const [dragPreviewPosition, setDragPreviewPosition] = useState<{ x: number; y: number } | null>(null);
  const transparentDragImage = React.useMemo(() => createTransparentDragImage(), []);

  const [snapToGridEnabled, setSnapToGridEnabled] = useState(true);
  const [guidesVisible, setGuidesVisible] = useState(true);
  const [contextMenu, setContextMenu] = useState<{ nodeId: string; x: number; y: number } | null>(null);
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);

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

  const beginAgentDrag = useCallback(
    (event: React.DragEvent, agent: { id: number; name: string }) => {
      event.stopPropagation();
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
        icon: "",
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
        icon: "",
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
    (event: React.DragEvent, tool: { type: string; name: string }) => {
      event.stopPropagation();
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
        icon: "",
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
        icon: "",
        baseSize: { width: 160, height: 48 },
        pointerRatio: { x: 0, y: 0 },
        toolType: tool.type,
      };
        setDragPreviewData(preview);
        updatePreviewPositionFromClientPoint({ x: event.clientX ?? 0, y: event.clientY ?? 0 }, preview);
      }
      setIsDragActive(true);
    },
    [setIsDragActive, transparentDragImage, updatePreviewPositionFromClientPoint]
  );

  const handleAgentPointerDown = useCallback(
    (event: React.PointerEvent, agent: { id: number; name: string }) => {
      // Only use Pointer API for touch/pen; let HTML5 drag handle mouse
      if (event.isPrimary && event.pointerType !== 'mouse') {
        startDrag(event, {
          type: 'agent',
          id: agent.id.toString(),
          name: agent.name
        });

        const rect = event.currentTarget.getBoundingClientRect();
        const pointerOffsetX = event.clientX - rect.left;
        const pointerOffsetY = event.clientY - rect.top;
      const preview: DragPreviewData = {
        kind: 'agent',
        label: agent.name,
        icon: '',
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
  },
  [startDrag, setDragPreviewData, updatePreviewPositionFromClientPoint, setIsDragActive]
);

const handleToolPointerDown = useCallback(
  (event: React.PointerEvent, tool: { type: string; name: string }) => {
    // Only use Pointer API for touch/pen; let HTML5 drag handle mouse
    if (event.isPrimary && event.pointerType !== 'mouse') {
      startDrag(event, {
        type: 'tool',
        name: tool.name,
        tool_type: tool.type
      });

      const rect = event.currentTarget.getBoundingClientRect();
      const pointerOffsetX = event.clientX - rect.left;
      const pointerOffsetY = event.clientY - rect.top;
      const preview: DragPreviewData = {
        kind: 'tool',
        label: tool.name,
        icon: '',
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
  },
  [startDrag, setDragPreviewData, updatePreviewPositionFromClientPoint, setIsDragActive]
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

  // Effect 2: Pointer event handlers for touch/pen drag
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
      const dragData = getDragData();
      if (!dragData) {
        return;
      }

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
    endDrag,
    updatePreviewPositionFromClientPoint,
  ]);

  // Fetch workflow by ID or name from URL param, or current workflow
  // If param is numeric, treat as ID; otherwise treat as name
  const isNumericId = workflowIdParam ? /^\d+$/.test(workflowIdParam) : false;
  const { data: workflow, isFetched: isWorkflowFetched } = useQuery<Workflow>({
    queryKey: workflowIdParam
      ? isNumericId
        ? ["workflow", parseInt(workflowIdParam, 10)]
        : ["workflow", "name", workflowIdParam]
      : ["workflow", "current"],
    queryFn: () => {
      if (!workflowIdParam) {
        return fetchCurrentWorkflow();
      }
      if (isNumericId) {
        return fetchWorkflowById(parseInt(workflowIdParam, 10));
      }
      return fetchWorkflowByName(workflowIdParam);
    },
    staleTime: 30000,
  });

  // Initialize nodes and edges from workflow data ONLY on first load
  React.useEffect(() => {
    if (workflow?.canvas && !canvasInitializedRef.current) {
      const { nodes: flowNodes, edges: flowEdges } = convertToReactFlowData(workflow.canvas);
      setNodes(flowNodes);
      setEdges(flowEdges);
      canvasInitializedRef.current = true;

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

  // Ready signal - indicates canvas is interactive (even if empty)
  // Used by E2E tests and marketing screenshots
  useEffect(() => {
    if (isWorkflowFetched) {
      document.body.setAttribute('data-ready', 'true');
    }
    return () => document.body.removeAttribute('data-ready');
  }, [isWorkflowFetched]);

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

      if (hash === lastSavedHashRef.current || pendingHashesRef.current.has(hash)) {
        return null;
      }

      pendingHashesRef.current.add(hash);
      const result = await updateWorkflowCanvas(data);
      return result;
    },
    onSuccess: (result, _variables, context) => {
      if (!result || !context) return;

      lastSavedHashRef.current = context.hash;
      pendingHashesRef.current.delete(context.hash);

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
      setExecutionLogs([]);
      return startWorkflowExecution(workflow.id);
    },
    onSuccess: (execution) => {
      console.log('[CanvasPage] 🎯 Workflow started, execution_id:', execution.execution_id);
      setCurrentExecution(execution);
      toast.success("Workflow execution started! Watch the logs panel for real-time updates.");
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
  const handleStreamingMessage = useCallback((envelope: WebSocketMessage) => {
    const message_type = envelope.type || (envelope as any).message_type;
    const data = envelope.data as any;

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
        const { node_id, phase, result, error_message } = data;
        const logType = error_message ? 'error' : (phase === 'running' ? 'node' : 'output');
        const logMessage = `NODE ${node_id} → ${phase.toUpperCase()}${result ? ` [${result}]` : ''}`;

        console.log('[CanvasPage] 📍 Node:', node_id, '→', phase, result || '');

        setExecutionLogs(prev => [...prev, {
          timestamp: Date.now(),
          type: logType,
          message: logMessage,
          metadata: data
        }]);
        break;
      }

      case 'workflow_progress': {
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

    sendMessage({
      type: 'subscribe',
      topics: [topic]
    });

    return () => {
      console.log('[CanvasPage] 🔕 Unsubscribing from topic:', topic);
      sendMessage({
        type: 'unsubscribe',
        topics: [topic]
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentExecution?.execution_id]);

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
    if (nodes.length > 0 || edges.length > 0) {
      const workflowData = normalizeWorkflow(nodes, edges);
      saveWorkflowMutation.mutate(workflowData);
    }
  }, [nodes, edges, saveWorkflowMutation]);

  // E2E Test Compatibility: Add legacy CSS classes to React Flow nodes
  useEffect(() => {
    const addCompatibilityClasses = () => {
      const reactFlowNodes = document.querySelectorAll('.react-flow__node');
      reactFlowNodes.forEach((node) => {
        node.classList.add('canvas-node', 'generic-node');
      });

      const reactFlowEdges = document.querySelectorAll('.react-flow__edge path');
      reactFlowEdges.forEach((edge) => {
        edge.classList.add('canvas-edge', 'edge');
      });
    };

    addCompatibilityClasses();
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
    [dragPreviewData, finalizeDrop, resetDragPreview, setIsDragActive, updatePreviewPositionFromClientPoint]
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

  // Keyboard shortcuts
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

  // Context menu handlers
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

  return (
    <>
      <AgentShelf
        onAgentDragStart={beginAgentDrag}
        onToolDragStart={beginToolDrag}
        onAgentPointerDown={handleAgentPointerDown}
        onToolPointerDown={handleToolPointerDown}
      />

      <div
        id="canvas-container"
        data-testid="canvas-container"
        className="canvas-container"
      >
        <div className="main-content-area">
          <ExecutionControls
            workflow={workflow}
            nodes={nodes}
            currentExecution={currentExecution}
            showLogs={showLogs}
            snapToGridEnabled={snapToGridEnabled}
            guidesVisible={guidesVisible}
            isPending={executeWorkflowMutation.isPending || cancelExecutionMutation.isPending}
            onRun={() => executeWorkflowMutation.mutate()}
            onCancel={() => cancelExecutionMutation.mutate()}
            onToggleLogs={() => setShowLogs(!showLogs)}
            onToggleSnapToGrid={() => setSnapToGridEnabled((prev) => !prev)}
            onToggleGuides={() => setGuidesVisible((prev) => !prev)}
          />

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
                          <div className="agent-icon">
                            {getNodeIcon("agent")}
                          </div>
                          <div className="agent-name">{dragPreviewData.label}</div>
                        </div>
                      ) : (
                        <div className="tool-node drag-preview-node">
                          <div className="tool-icon">
                            {getNodeIcon("tool", dragPreviewData.toolType)}
                          </div>
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
                    backgroundColor: '#2a2a3a',
                    height: 120,
                    width: 160,
                    border: '1px solid #3d3d5c',
                    borderRadius: '4px'
                  }}
                />
              </ReactFlow>
            </div>
            <ExecutionLogsPanel
              showLogs={showLogs}
              currentExecution={currentExecution}
              executionLogs={executionLogs}
              onClose={() => setShowLogs(false)}
            />
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
                <XIcon width={14} height={14} />
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
