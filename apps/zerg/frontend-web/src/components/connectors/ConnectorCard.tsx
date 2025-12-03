/**
 * Shared ConnectorCard component for both agent-level and account-level connectors.
 * Supports both OAuth flow and manual credential entry.
 */

import type { ConnectorStatus, AccountConnectorStatus } from "../../types/connectors";

// Connectors that support OAuth flow
const OAUTH_CONNECTORS = ["github"] as const;
type OAuthConnector = (typeof OAUTH_CONNECTORS)[number];

function isOAuthConnector(type: string): type is OAuthConnector {
  return OAUTH_CONNECTORS.includes(type as OAuthConnector);
}

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
  const statusClass = connector.configured
    ? connector.test_status === "success"
      ? "status-success"
      : connector.test_status === "failed"
      ? "status-failed"
      : "status-untested"
    : "status-unconfigured";

  const statusText = connector.configured
    ? connector.test_status === "success"
      ? "Connected"
      : connector.test_status === "failed"
      ? "Failed"
      : "Untested"
    : "Not configured";

  const connectedViaOAuth = connector.metadata?.connected_via === "oauth";
  const supportsOAuth = isOAuthConnector(connector.type);

  return (
    <div className={`connector-card ${statusClass}`}>
      <div className="connector-card-header">
        <span className="connector-name">{connector.name}</span>
        <span className={`connector-status ${statusClass}`}>{statusText}</span>
      </div>

      {connector.configured && connector.display_name && (
        <div className="connector-display-name">{connector.display_name}</div>
      )}

      {connector.configured && connector.metadata && (
        <div className="connector-metadata">
          {Object.entries(connector.metadata)
            .filter(([k]) => !["enabled", "from_email", "from_number", "connected_via"].includes(k))
            .slice(0, 2)
            .map(([key, value]) => (
              <span key={key} className="metadata-item">
                {String(value)}
              </span>
            ))}
        </div>
      )}

      <div className="connector-card-actions">
        {connector.configured ? (
          <>
            {connectedViaOAuth && onOAuthConnect ? (
              <button
                type="button"
                className="btn-secondary"
                onClick={onOAuthConnect}
                disabled={isOAuthPending}
              >
                {isOAuthPending ? "Connecting..." : "Reconnect"}
              </button>
            ) : (
              <button type="button" className="btn-secondary" onClick={onConfigure}>
                Edit
              </button>
            )}
            <button type="button" className="btn-tertiary" onClick={onTest} disabled={isTesting}>
              Test
            </button>
            <button type="button" className="btn-danger" onClick={onDelete}>
              Remove
            </button>
          </>
        ) : supportsOAuth && onOAuthConnect ? (
          <button
            type="button"
            className="btn-primary"
            onClick={onOAuthConnect}
            disabled={isOAuthPending}
          >
            {isOAuthPending ? "Connecting..." : `Connect ${connector.name}`}
          </button>
        ) : (
          <button type="button" className="btn-primary" onClick={onConfigure}>
            Configure
          </button>
        )}
      </div>
    </div>
  );
}

export { isOAuthConnector };
export type { OAuthConnector };
