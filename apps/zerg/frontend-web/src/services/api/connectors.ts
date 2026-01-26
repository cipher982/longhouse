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

// Fiche-level connectors
export async function fetchFicheConnectors(ficheId: number): Promise<ConnectorStatus[]> {
  return request<ConnectorStatus[]>(`/fiches/${ficheId}/connectors`);
}

export async function configureFicheConnector(
  ficheId: number,
  payload: ConnectorConfigureRequest
): Promise<ConnectorSuccessResponse> {
  return request<ConnectorSuccessResponse>(`/fiches/${ficheId}/connectors`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testFicheConnectorBeforeSave(
  ficheId: number,
  payload: ConnectorTestRequest
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/fiches/${ficheId}/connectors/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function testFicheConnector(
  ficheId: number,
  connectorType: string
): Promise<ConnectorTestResponse> {
  return request<ConnectorTestResponse>(`/fiches/${ficheId}/connectors/${connectorType}/test`, {
    method: "POST",
  });
}

export async function deleteFicheConnector(
  ficheId: number,
  connectorType: string
): Promise<void> {
  return request<void>(`/fiches/${ficheId}/connectors/${connectorType}`, {
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
