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
