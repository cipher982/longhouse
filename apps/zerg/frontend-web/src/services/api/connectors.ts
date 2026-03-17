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

// Automation-level connectors
export async function fetchAutomationConnectors(automationId: number): Promise<ConnectorStatus[]> {
  return request<ConnectorStatus[]>(`/automations/${automationId}/connectors`);
}

export async function configureAutomationConnector(
  automationId: number,
  payload: ConnectorConfigureRequest
): Promise<ConnectorSuccessResponse> {
  return request<ConnectorSuccessResponse>(`/automations/${automationId}/connectors`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testAutomationConnectorBeforeSave(
  automationId: number,
  payload: ConnectorTestRequest
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/automations/${automationId}/connectors/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testAutomationConnector(
  automationId: number,
  connectorType: string
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/automations/${automationId}/connectors/${connectorType}/test`, {
    method: "POST",
  });
}

export async function deleteAutomationConnector(
  automationId: number,
  connectorType: string
): Promise<void> {
  return request<void>(`/automations/${automationId}/connectors/${connectorType}`, {
    method: "DELETE",
  });
}

export const fetchFicheConnectors = fetchAutomationConnectors;
export const configureFicheConnector = configureAutomationConnector;
export const testFicheConnectorBeforeSave = testAutomationConnectorBeforeSave;
export const testFicheConnector = testAutomationConnector;
export const deleteFicheConnector = deleteAutomationConnector;

// Account-level connectors
export async function fetchAccountConnectors(): Promise<AccountConnectorStatus[]> {
  return request<AccountConnectorStatus[]>(`/account/connectors/`);
}

export async function configureAccountConnector(
  payload: ConnectorConfigureRequest
): Promise<ConnectorSuccessResponse> {
  return request<ConnectorSuccessResponse>(`/account/connectors/`, {
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
