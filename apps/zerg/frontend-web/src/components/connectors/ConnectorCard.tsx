/**
 * Shared ConnectorCard component for both agent-level and account-level connectors.
 * Supports both OAuth flow and manual credential entry.
 */

import type { ConnectorStatus, AccountConnectorStatus } from "../../types/connectors";
import { Button, Card, Badge } from "../ui";

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

  const getStatusVariant = (): 'success' | 'error' | 'warning' | 'neutral' => {
    if (!connector.configured) return 'neutral';
    if (connector.test_status === 'success') return 'success';
    if (connector.test_status === 'failed') return 'error';
    return 'warning';
  };

  return (
    <Card className={`connector-card ${statusClass}`}>
      <Card.Header>
        <span className="connector-name" style={{ fontWeight: 600 }}>{connector.name}</span>
        <Badge variant={getStatusVariant()}>{statusText}</Badge>
      </Card.Header>

      <Card.Body>
        {connector.configured && connector.display_name && (
          <div className="connector-display-name" style={{ marginBottom: 'var(--space-2)', fontSize: '0.9rem' }}>{connector.display_name}</div>
        )}

        {connector.configured && connector.metadata && (
          <div className="connector-metadata" style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-4)' }}>
            {Object.entries(connector.metadata)
              .filter(([k]) => !["enabled", "from_email", "from_number", "connected_via"].includes(k))
              .slice(0, 2)
              .map(([key, value]) => (
                <Badge key={key} variant="neutral" style={{ textTransform: 'none', fontSize: '10px' }}>
                  {String(value)}
                </Badge>
              ))}
          </div>
        )}

        <div className="connector-card-actions" style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
          {connector.configured ? (
            <>
              {connectedViaOAuth && onOAuthConnect ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={onOAuthConnect}
                  disabled={isOAuthPending}
                >
                  {isOAuthPending ? "Connecting..." : "Reconnect"}
                </Button>
              ) : (
                <Button variant="secondary" size="sm" onClick={onConfigure}>
                  Edit
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={onTest} disabled={isTesting}>
                Test
              </Button>
              <Button variant="danger" size="sm" onClick={onDelete}>
                Remove
              </Button>
            </>
          ) : supportsOAuth && onOAuthConnect ? (
            <Button
              variant="primary"
              size="sm"
              onClick={onOAuthConnect}
              disabled={isOAuthPending}
              style={{ width: '100%' }}
            >
              {isOAuthPending ? "Connecting..." : `Connect ${connector.name}`}
            </Button>
          ) : (
            <Button variant="primary" size="sm" onClick={onConfigure} style={{ width: '100%' }}>
              Configure
            </Button>
          )}
        </div>
      </Card.Body>
    </Card>
  );
}

export { isOAuthConnector };
export type { OAuthConnector };
