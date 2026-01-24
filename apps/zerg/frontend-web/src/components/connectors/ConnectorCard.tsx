/**
 * Shared ConnectorCard component for both agent-level and account-level connectors.
 * Compact design with service icons and inline actions.
 */

import type { ReactNode } from "react";
import type { ConnectorStatus, AccountConnectorStatus } from "../../types/connectors";
import { Button, Badge } from "../ui";
import {
  SlackIcon,
  DiscordIcon,
  SmartphoneIcon,
  MessageSquareIcon,
  GithubIcon,
  JiraIcon,
  NotionIcon,
  ClipboardListIcon,
  MapPinIcon,
  HeartIcon,
  FileTextIcon,
  MailIcon,
  PlugIcon,
  CheckCircleIcon,
  SettingsIcon,
  TrashIcon,
  ZapIcon,
} from "../icons";

// Connectors that support OAuth flow
const OAUTH_CONNECTORS = ["github"] as const;
type OAuthConnector = (typeof OAUTH_CONNECTORS)[number];

function isOAuthConnector(type: string): type is OAuthConnector {
  return OAUTH_CONNECTORS.includes(type as OAuthConnector);
}

// Icon and color mapping for each connector type
const iconSize = { width: 20, height: 20 };
const CONNECTOR_CONFIG: Record<string, { icon: ReactNode; color: string }> = {
  slack: { icon: <SlackIcon {...iconSize} />, color: "#4A154B" },
  discord: { icon: <DiscordIcon {...iconSize} />, color: "#5865F2" },
  twilio: { icon: <SmartphoneIcon {...iconSize} />, color: "#F22F46" },
  imessage: { icon: <MessageSquareIcon {...iconSize} />, color: "#34C759" },
  github: { icon: <GithubIcon {...iconSize} />, color: "#6e5494" },
  jira: { icon: <JiraIcon {...iconSize} />, color: "#0052CC" },
  linear: { icon: <ClipboardListIcon {...iconSize} />, color: "#5E6AD2" },
  notion: { icon: <NotionIcon {...iconSize} />, color: "#FFFFFF" },
  google_maps: { icon: <MapPinIcon {...iconSize} />, color: "#4285F4" },
  oura: { icon: <HeartIcon {...iconSize} />, color: "#00C896" },
  obsidian: { icon: <FileTextIcon {...iconSize} />, color: "#7C3AED" },
  sendgrid: { icon: <MailIcon {...iconSize} />, color: "#1A82E2" },
  email: { icon: <MailIcon {...iconSize} />, color: "#EA4335" },
};

const DEFAULT_CONFIG = { icon: <PlugIcon {...iconSize} />, color: "#6366f1" };

type ConnectorCardProps = {
  connector: ConnectorStatus | AccountConnectorStatus;
  onConfigure: () => void;
  onOAuthConnect?: () => void;
  onTest: () => void;
  onDelete: () => void;
  isTesting: boolean;
  isOAuthPending?: boolean;
};

export function ConnectorCard({
  connector,
  onConfigure,
  onOAuthConnect,
  onTest,
  onDelete,
  isTesting,
  isOAuthPending,
}: ConnectorCardProps) {
  const config = CONNECTOR_CONFIG[connector.type] ?? DEFAULT_CONFIG;
  const connectedViaOAuth = connector.metadata?.connected_via === "oauth";
  const supportsOAuth = isOAuthConnector(connector.type);

  return (
    <div className={`integration-card ${connector.configured ? "configured" : ""}`}>
      <div className="integration-card-icon" style={{ backgroundColor: config.color }}>
        {config.icon}
      </div>

      <div className="integration-card-content">
        <div className="integration-card-header">
          <span className="integration-name">{connector.name}</span>
          {connector.configured ? (
            <span className="integration-status connected">
              <CheckCircleIcon width={14} height={14} />
              {connector.test_status === "success" ? "Connected" : connector.test_status === "failed" ? "Error" : "Ready"}
            </span>
          ) : (
            <span className="integration-status">Not configured</span>
          )}
        </div>

        {connector.configured && connector.display_name && (
          <span className="integration-label">{connector.display_name}</span>
        )}

        {connector.configured && connector.metadata && (
          <div className="integration-meta">
            {Object.entries(connector.metadata)
              .filter(([k]) => !["enabled", "from_email", "from_number", "connected_via"].includes(k))
              .slice(0, 1)
              .map(([key, value]) => (
                <span key={key} className="integration-meta-value">{String(value)}</span>
              ))}
          </div>
        )}
      </div>

      <div className="integration-card-actions">
        {connector.configured ? (
          <>
            {connectedViaOAuth && onOAuthConnect ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={onOAuthConnect}
                disabled={isOAuthPending}
                title="Reconnect"
              >
                <SettingsIcon width={16} height={16} />
              </Button>
            ) : (
              <Button variant="ghost" size="sm" onClick={onConfigure} title="Edit">
                <SettingsIcon width={16} height={16} />
              </Button>
            )}
            <Button variant="ghost" size="sm" onClick={onTest} disabled={isTesting} title="Test connection">
              <ZapIcon width={16} height={16} />
            </Button>
            <Button variant="ghost" size="sm" onClick={onDelete} title="Remove" className="danger-hover">
              <TrashIcon width={16} height={16} />
            </Button>
          </>
        ) : supportsOAuth && onOAuthConnect ? (
          <Button
            variant="primary"
            size="sm"
            onClick={onOAuthConnect}
            disabled={isOAuthPending}
          >
            {isOAuthPending ? "..." : "Connect"}
          </Button>
        ) : (
          <Button variant="secondary" size="sm" onClick={onConfigure}>
            Configure
          </Button>
        )}
      </div>
    </div>
  );
}

export { isOAuthConnector };
export type { OAuthConnector };
