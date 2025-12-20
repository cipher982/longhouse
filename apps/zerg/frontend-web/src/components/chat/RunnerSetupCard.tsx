import { useState, useEffect, useCallback, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { fetchRunners, type Runner } from "../../services/api";
import { SyntaxHighlighter, oneDark } from "../../lib/syntaxHighlighter";

interface RunnerSetupData {
  enroll_token: string;
  expires_at: string;
  swarmlet_url: string;
  docker_command: string;
}

interface RunnerSetupCardProps {
  data: RunnerSetupData;
  rawContent?: string; // For debugging: show raw JSON output
}

type ConnectionStatus = "waiting" | "connected" | "expired";

export function RunnerSetupCard({ data, rawContent }: RunnerSetupCardProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("waiting");
  const [connectedRunner, setConnectedRunner] = useState<Runner | null>(null);
  const [timeRemaining, setTimeRemaining] = useState("");
  const [baselineRunnerIds, setBaselineRunnerIds] = useState<Set<number> | null>(null);
  const [showRawOutput, setShowRawOutput] = useState(false);
  const queryClient = useQueryClient();

  // Track when the token was generated (card render time).
  // Used to verify runners were enrolled via THIS token, not pre-existing.
  const tokenCreatedAt = useRef(new Date());

  const copyToClipboard = async (text: string): Promise<boolean> => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch {
      // fall through to legacy copy method
    }

    // Fallback for insecure contexts / denied permissions
    try {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      textarea.style.top = "0";
      document.body.appendChild(textarea);
      textarea.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(textarea);
      return ok;
    } catch {
      return false;
    }
  };

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

    // Capture baseline of ALL runner IDs on first poll.
    // To detect the runner enrolled via THIS token, we check:
    // 1. Runner ID is NOT in the baseline (brand new runner)
    // 2. Runner was created AFTER the token was generated
    // 3. Runner is currently online
    // This prevents false positives from existing runners reconnecting.
    const pollForNewRunner = async () => {
      try {
        const runners = await fetchRunners();

        if (baselineRunnerIds === null) {
          // First poll - capture ALL runner IDs as baseline
          const allIds = new Set(runners.map((r) => r.id));
          setBaselineRunnerIds(allIds);
          return;
        }

        // Find runner that:
        // - Is online
        // - Is NOT in baseline (brand new)
        // - Was created AFTER the token was generated
        const newEnrolledRunner = runners.find((r) => {
          if (r.status !== "online") return false;
          if (baselineRunnerIds.has(r.id)) return false;

          // Verify runner was created after token generation
          const runnerCreatedAt = new Date(r.created_at);
          if (runnerCreatedAt < tokenCreatedAt.current) return false;

          return true;
        });

        if (newEnrolledRunner) {
          setConnectedRunner(newEnrolledRunner);
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
  }, [status, baselineRunnerIds, queryClient]);

  const handleCopy = async () => {
    setCopyError(null);
    const ok = await copyToClipboard(data.docker_command);
    if (!ok) {
      setCopyError("Copy failed. Select the command and copy it manually.");
      return;
    }
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

        {copyError && <div className="runner-setup-copy-error">{copyError}</div>}

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

        {rawContent && (
          <div className="runner-setup-raw">
            <button
              type="button"
              className="runner-setup-raw-toggle"
              onClick={() => setShowRawOutput(!showRawOutput)}
            >
              {showRawOutput ? "▼ Hide raw output" : "▶ Show raw output"}
            </button>
            {showRawOutput && (
              <div className="runner-setup-raw-content">
                <SyntaxHighlighter
                  language="json"
                  style={oneDark}
                  customStyle={{ margin: 0, borderRadius: "4px", fontSize: "12px" }}
                  wrapLongLines={true}
                >
                  {rawContent}
                </SyntaxHighlighter>
              </div>
            )}
          </div>
        )}
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
