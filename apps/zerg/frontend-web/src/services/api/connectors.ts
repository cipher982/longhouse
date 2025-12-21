import { request } from "./base";
import type {
  ConnectorStatus,
  ConnectorConfigureRequest,
  ConnectorTestRequest,
  ConnectorTestResponse,
  ConnectorSuccessResponse,
  AccountConnectorStatus,
} from "../../types/connectors";

export type {
  ConnectorStatus,
  ConnectorConfigureRequest,
  ConnectorTestRequest,
  ConnectorTestResponse,
  ConnectorSuccessResponse,
  AccountConnectorStatus,
};

// Agent-level connectors
export async function fetchAgentConnectors(agentId: number): Promise<ConnectorStatus[]> {
  return request<ConnectorStatus[]>(`/agents/${agentId}/connectors`);
}

export async function configureAgentConnector(
  agentId: number,
  payload: ConnectorConfigureRequest
): Promise<ConnectorSuccessResponse> {
  return request<ConnectorSuccessResponse>(`/agents/${agentId}/connectors`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testAgentConnectorBeforeSave(
  agentId: number,
  payload: ConnectorTestRequest
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/agents/${agentId}/connectors/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testAgentConnector(
  agentId: number,
  connectorType: string
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/agents/${agentId}/connectors/${connectorType}/test`, {
    method: "POST",
  });
}

export async function deleteAgentConnector(
  agentId: number,
  connectorType: string
): Promise<void> {
  return request<void>(`/agents/${agentId}/connectors/${connectorType}`, {
    method: "DELETE",
  });
}

// Account-level connectors
export async function fetchAccountConnectors(): Promise<AccountConnectorStatus[]> {
  return request<AccountConnectorStatus[]>(`/account/connectors`);
}

export async function configureAccountConnector(
  payload: ConnectorConfigureRequest
): Promise<ConnectorSuccessResponse> {
  return request<ConnectorSuccessResponse>(`/account/connectors`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testAccountConnectorBeforeSave(
  payload: ConnectorTestRequest
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/account/connectors/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testAccountConnector(
  connectorType: string
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/account/connectors/${connectorType}/test`, {
    method: "POST",
  });
}

export async function deleteAccountConnector(
  connectorType: string
): Promise<void> {
  return request<void>(`/account/connectors/${connectorType}`, {
    method: "DELETE",
  });
}
