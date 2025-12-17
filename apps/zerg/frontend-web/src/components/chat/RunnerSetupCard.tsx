import { useState, useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { fetchRunners, type Runner } from "../../services/api";

interface RunnerSetupData {
  enroll_token: string;
  expires_at: string;
  swarmlet_url: string;
  docker_command: string;
}

interface RunnerSetupCardProps {
  data: RunnerSetupData;
}

type ConnectionStatus = "waiting" | "connected" | "expired";

export function RunnerSetupCard({ data }: RunnerSetupCardProps) {
  const [copied, setCopied] = useState(false);
  const [status, setStatus] = useState<ConnectionStatus>("waiting");
  const [connectedRunner, setConnectedRunner] = useState<Runner | null>(null);
  const [timeRemaining, setTimeRemaining] = useState("");
  const [initialRunnerIds, setInitialRunnerIds] = useState<Set<number> | null>(null);
  const queryClient = useQueryClient();

  // Calculate time remaining
  const updateTimeRemaining = useCallback(() => {
    const expiry = new Date(data.expires_at);
    const now = new Date();
    const diffMs = expiry.getTime() - now.getTime();

    if (diffMs <= 0) {
      setTimeRemaining("Expired");
      setStatus("expired");
      return;
    }

    const minutes = Math.floor(diffMs / 60000);
    const seconds = Math.floor((diffMs % 60000) / 1000);
    setTimeRemaining(`${minutes}:${seconds.toString().padStart(2, "0")}`);
  }, [data.expires_at]);

  // Timer for countdown
  useEffect(() => {
    if (status === "connected" || status === "expired") return;

    updateTimeRemaining();
    const interval = setInterval(updateTimeRemaining, 1000);
    return () => clearInterval(interval);
  }, [status, updateTimeRemaining]);

  // Poll for new runners
  useEffect(() => {
    if (status !== "waiting") return;

    // Capture initial runner IDs on first poll
    const pollForNewRunner = async () => {
      try {
        const runners = await fetchRunners();
        const currentIds = new Set(runners.map((r) => r.id));

        if (initialRunnerIds === null) {
          // First poll - capture baseline
          setInitialRunnerIds(currentIds);
          return;
        }

        // Find new online runners
        const newOnlineRunner = runners.find(
          (r) => r.status === "online" && !initialRunnerIds.has(r.id)
        );

        if (newOnlineRunner) {
          setConnectedRunner(newOnlineRunner);
          setStatus("connected");
          // Invalidate runners query to refresh the list
          queryClient.invalidateQueries({ queryKey: ["runners"] });
        }
      } catch (error) {
        console.error("Failed to poll for runners:", error);
      }
    };

    // Poll every 3 seconds
    pollForNewRunner();
    const interval = setInterval(pollForNewRunner, 3000);
    return () => clearInterval(interval);
  }, [status, initialRunnerIds, queryClient]);

  const handleCopy = () => {
    navigator.clipboard.writeText(data.docker_command);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (status === "connected" && connectedRunner) {
    return (
      <div className="runner-setup-card runner-setup-card-success">
        <div className="runner-setup-header">
          <span className="runner-setup-icon">✅</span>
          <span className="runner-setup-title">Runner Connected!</span>
        </div>
        <div className="runner-setup-body">
          <div className="runner-connected-info">
            <div className="runner-connected-name">
              <strong>"{connectedRunner.name}"</strong> is now online and ready
            </div>
            <div className="runner-connected-details">
              <span className="runner-detail">
                <span className="runner-detail-label">Capabilities:</span>{" "}
                {connectedRunner.capabilities?.join(", ") || "exec.readonly"}
              </span>
              <span className="runner-detail">
                <span className="runner-detail-label">Status:</span>{" "}
                <span className="runner-status-online">● Online</span>
              </span>
            </div>
          </div>
          <div className="runner-setup-actions">
            <a
              href={`/runners/${connectedRunner.id}`}
              className="runner-action-button runner-action-secondary"
            >
              Configure Runner
            </a>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`runner-setup-card ${status === "expired" ? "runner-setup-card-expired" : ""}`}>
      <div className="runner-setup-header">
        <span className="runner-setup-icon">🖥️</span>
        <span className="runner-setup-title">Connect Your Infrastructure</span>
      </div>

      <div className="runner-setup-body">
        <p className="runner-setup-description">
          Run this on your machine to connect:
        </p>

        <div className="runner-setup-code-container">
          <pre className="runner-setup-code">
            <code>{data.docker_command}</code>
          </pre>
          <button
            type="button"
            className="runner-setup-copy-button"
            onClick={handleCopy}
            disabled={status === "expired"}
          >
            {copied ? "✓ Copied" : "📋 Copy"}
          </button>
        </div>

        <div className="runner-setup-status">
          {status === "waiting" && (
            <>
              <span className="runner-status-waiting">
                <span className="runner-status-spinner">⏳</span>
                Waiting for connection...
              </span>
              <span className="runner-status-timer">
                Token expires in: <strong>{timeRemaining}</strong>
              </span>
            </>
          )}
          {status === "expired" && (
            <span className="runner-status-expired">
              ⚠️ Token expired. Ask me to generate a new one.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Parse tool output to extract runner setup data
 */
export function parseRunnerSetupData(content: string): RunnerSetupData | null {
  try {
    const parsed = JSON.parse(content);

    // Check if this is a successful runner_create_enroll_token response
    if (
      parsed?.ok === true &&
      parsed?.data?.enroll_token &&
      parsed?.data?.docker_command
    ) {
      return {
        enroll_token: parsed.data.enroll_token,
        expires_at: parsed.data.expires_at,
        swarmlet_url: parsed.data.swarmlet_url,
        docker_command: parsed.data.docker_command,
      };
    }
  } catch {
    // Not JSON or invalid format
  }
  return null;
}
