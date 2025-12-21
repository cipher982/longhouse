import { request } from "./base";
import type { Workflow, WorkflowDataInput, ExecutionStatus, ExecutionLogs, WorkflowExecution } from "./types";

type WorkflowCreate = {
  name: string;
  description: string;
  canvas: WorkflowDataInput;
};

type CanvasUpdate = {
  canvas: WorkflowDataInput;
};

export async function fetchWorkflows(): Promise<Workflow[]> {
  return request<Workflow[]>(`/workflows`);
}

export async function fetchCurrentWorkflow(): Promise<Workflow> {
  return request<Workflow>(`/workflows/current`);
}

export async function createWorkflow(name: string, description?: string, canvas?: WorkflowDataInput): Promise<Workflow> {
  const payload: WorkflowCreate = {
    name,
    description: description || "",
    canvas: canvas || { nodes: [], edges: [] },
  };
  return request<Workflow>(`/workflows`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateWorkflowCanvas(canvas: WorkflowDataInput): Promise<Workflow> {
  const payload: CanvasUpdate = {
    canvas,
  };
  return request<Workflow>(`/workflows/current/canvas`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function reserveWorkflowExecution(workflowId: number): Promise<ExecutionStatus> {
  return request<ExecutionStatus>(`/workflow-executions/by-workflow/${workflowId}/reserve`, {
    method: "POST",
  });
}

export async function startWorkflowExecution(workflowId: number): Promise<ExecutionStatus> {
  return request<ExecutionStatus>(`/workflow-executions/by-workflow/${workflowId}/start`, {
    method: "POST",
  });
}

export async function startReservedExecution(executionId: number): Promise<ExecutionStatus> {
  return request<ExecutionStatus>(`/workflow-executions/executions/${executionId}/start`, {
    method: "POST",
  });
}

export async function getExecutionStatus(executionId: number): Promise<ExecutionStatus> {
  return request<ExecutionStatus>(`/workflow-executions/${executionId}/status`);
}

export async function getExecutionLogs(executionId: number): Promise<ExecutionLogs> {
  return request<ExecutionLogs>(`/workflow-executions/${executionId}/logs`);
}

export async function cancelExecution(executionId: number, reason: string): Promise<void> {
  return request<void>(`/workflow-executions/${executionId}/cancel`, {
    method: "PATCH",
    body: JSON.stringify({ reason }),
  });
}

export async function getExecutionHistory(workflowId: number): Promise<WorkflowExecution[]> {
  return request<WorkflowExecution[]>(`/workflow-executions/history/${workflowId}`);
}
