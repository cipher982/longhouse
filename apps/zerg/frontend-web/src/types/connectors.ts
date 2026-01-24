/**
 * TypeScript types for agent connector credentials.
 */

export interface CredentialField {
  key: string;
  label: string;
  type: "text" | "password" | "url";
  placeholder: string;
  required: boolean;
}

export interface ConnectorStatus {
  type: string;
  name: string;
  description: string;
  category: "notifications" | "project_management" | "personal" | "advanced";
  icon: string;
  docs_url: string;
  fields: CredentialField[];
  configured: boolean;
  display_name: string | null;
  test_status: "untested" | "success" | "failed";
  last_tested_at: string | null;
  metadata: Record<string, unknown> | null;
}

export interface ConnectorConfigureRequest {
  connector_type: string;
  credentials: Record<string, string>;
  display_name?: string;
}

export interface ConnectorTestRequest {
  connector_type: string;
  credentials: Record<string, string>;
}

export interface ConnectorTestResponse {
  success: boolean;
  message: string;
  metadata?: Record<string, unknown>;
}

export interface ConnectorSuccessResponse {
  success: boolean;
}

/**
 * Account-level connector status (same structure as agent-level).
 * Used for account settings integrations page.
 */
export type AccountConnectorStatus = ConnectorStatus;

export type ConnectorCategory = "notifications" | "project_management" | "personal" | "advanced";

// Connector type identifiers
export type ConnectorType =
  | "slack"
  | "discord"
  | "email"
  | "sms"
  | "github"
  | "jira"
  | "linear"
  | "notion"
  | "imessage"
  | "traccar"
  | "whoop"
  | "obsidian";

// Icon mapping for connectors (Lucide icon names)
export const CONNECTOR_ICONS: Record<string, string> = {
  slack: "slack",
  discord: "discord",
  email: "mail",
  sms: "smartphone",
  github: "github",
  jira: "clipboard",
  linear: "layout",
  notion: "file-text",
  imessage: "message-circle",
  traccar: "map-pin",
  whoop: "activity",
  obsidian: "book-open",
};
