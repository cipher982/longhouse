import { useState, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchRunners, type Runner } from "../../services/api";
import { SyntaxHighlighter, oneDark } from "../../lib/syntaxHighlighter";
import { CheckCircleIcon, MonitorIcon, ClipboardIcon, AlertTriangleIcon, ChevronRightIcon, ChevronDownIcon } from "../icons";
import { parseUTC } from "../../lib/dateUtils";
import { buildRunnerNativeInstallCommand, describeRunnerNativeInstallMode, type RunnerNativeInstallMode } from "../../lib/runnerInstallCommands";

interface RunnerSetupData {
  enroll_token: string;
  expires_at: string;
  longhouse_url: string;
  docker_command: string;
  one_liner_install_command: string;
}

interface RunnerSetupCardProps {
  data: RunnerSetupData;
  rawContent?: string; // For debugging: show raw JSON output
}

type ConnectionStatus = "waiting" | "connected" | "expired";

function formatTimeRemaining(expiresAt: string, nowMs: number) {
  const expiry = parseUTC(expiresAt);
  const diffMs = expiry.getTime() - nowMs;

  if (diffMs <= 0) {
    return "Expired";
  }

  const minutes = Math.floor(diffMs / 60000);
  const seconds = Math.floor((diffMs % 60000) / 1000);
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function useNow(enabled: boolean, intervalMs = 1000) {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!enabled) {
      return;
    }

    setNowMs(Date.now());
    const interval = window.setInterval(() => {
      setNowMs(Date.now());
    }, intervalMs);

    return () => window.clearInterval(interval);
  }, [enabled, intervalMs]);

  return nowMs;
}

function findNewEnrolledRunner(runners: Runner[], baselineRunnerIds: Set<number>, tokenCreatedAt: Date) {
  return runners.find((runner) => {
    if (runner.status !== "online") return false;
    if (baselineRunnerIds.has(runner.id)) return false;

    const runnerCreatedAt = parseUTC(runner.created_at);
    return runnerCreatedAt >= tokenCreatedAt;
  }) ?? null;
}

export function RunnerSetupCard({ data, rawContent }: RunnerSetupCardProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [nativeMode, setNativeMode] = useState<RunnerNativeInstallMode>("desktop");
  const [showRawOutput, setShowRawOutput] = useState(false);
  const [showManualSetup, setShowManualSetup] = useState(false);

  // Track when the token was generated (card render time).
  // Used to verify runners were enrolled via THIS token, not pre-existing.
  const tokenKeyRef = useRef(data.enroll_token);
  const tokenCreatedAt = useRef(new Date());
  const baselineRunnerIdsRef = useRef<Set<number> | null>(null);

  if (tokenKeyRef.current !== data.enroll_token) {
    tokenKeyRef.current = data.enroll_token;
    tokenCreatedAt.current = new Date();
    baselineRunnerIdsRef.current = null;
  }

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

  const nowMs = useNow(true);
  const timeRemaining = formatTimeRemaining(data.expires_at, nowMs);
  const isExpired = timeRemaining === "Expired";

  const enrolledRunnerQuery = useQuery<Runner | null>({
    queryKey: ["runner-setup-enrollment", data.enroll_token, data.expires_at],
    enabled: !isExpired,
    refetchInterval: 3000,
    refetchOnWindowFocus: false,
    retry: false,
    placeholderData: (previousRunner) => previousRunner,
    queryFn: async () => {
      const runners = await fetchRunners();

      if (baselineRunnerIdsRef.current === null) {
        baselineRunnerIdsRef.current = new Set(runners.map((runner) => runner.id));
        return null;
      }

      return findNewEnrolledRunner(
        runners,
        baselineRunnerIdsRef.current,
        tokenCreatedAt.current,
      );
    },
  });

  const connectedRunner = enrolledRunnerQuery.data ?? null;
  const status: ConnectionStatus = connectedRunner
    ? "connected"
    : isExpired
      ? "expired"
      : "waiting";

  const nativeInstallCommand = buildRunnerNativeInstallCommand({
    enrollToken: data.enroll_token,
    longhouseUrl: data.longhouse_url,
    oneLinerInstallCommand: data.one_liner_install_command,
  }, nativeMode);

  const handleCopyOneLiner = async () => {
    setCopyError(null);
    const ok = await copyToClipboard(nativeInstallCommand);
    if (!ok) {
      setCopyError("Copy failed. Select the command and copy it manually.");
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleCopyManual = async () => {
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
          <span className="runner-setup-icon"><CheckCircleIcon width={20} height={20} /></span>
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
        <span className="runner-setup-icon"><MonitorIcon width={20} height={20} /></span>
        <span className="runner-setup-title">Connect Your Infrastructure</span>
      </div>

      <div className="runner-setup-body">
        <p className="runner-setup-description">
          <strong>One-liner install (recommended):</strong>
        </p>

        <p className="runner-setup-mode-label">Machine type:</p>
        <div className="runner-setup-mode-tabs">
          <button
            type="button"
            className={`runner-setup-mode-tab${nativeMode === "desktop" ? " runner-setup-mode-tab--active" : ""}`}
            onClick={() => { setNativeMode("desktop"); setCopied(false); setCopyError(null); }}
            disabled={status === "expired"}
          >
            Desktop / Laptop
          </button>
          <button
            type="button"
            className={`runner-setup-mode-tab${nativeMode === "server" ? " runner-setup-mode-tab--active" : ""}`}
            onClick={() => { setNativeMode("server"); setCopied(false); setCopyError(null); }}
            disabled={status === "expired"}
          >
            Always-on Linux Server
          </button>
        </div>
        <p className="runner-setup-mode-note">{describeRunnerNativeInstallMode(nativeMode)}</p>

        <div className="runner-setup-code-container">
          <pre className="runner-setup-code">
            <code>{nativeInstallCommand}</code>
          </pre>
          <button
            type="button"
            className="runner-setup-copy-button"
            onClick={handleCopyOneLiner}
            disabled={status === "expired"}
          >
            {copied ? "✓ Copied" : <><ClipboardIcon width={14} height={14} /> Copy</>}
          </button>
        </div>

        {copyError && <div className="runner-setup-copy-error">{copyError}</div>}

        <div className="runner-setup-manual-toggle">
          <button
            type="button"
            className="runner-setup-manual-toggle-button"
            onClick={() => setShowManualSetup(!showManualSetup)}
          >
            {showManualSetup ? <><ChevronDownIcon width={14} height={14} /> Hide manual setup (2 steps)</> : <><ChevronRightIcon width={14} height={14} /> Show manual setup (2 steps)</>}
          </button>
        </div>

        {showManualSetup && (
          <div className="runner-setup-manual-section">
            <p className="runner-setup-description">
              <strong>Manual setup (2 steps):</strong>
            </p>
            <div className="runner-setup-code-container">
              <pre className="runner-setup-code">
                <code>{data.docker_command}</code>
              </pre>
              <button
                type="button"
                className="runner-setup-copy-button"
                onClick={handleCopyManual}
                disabled={status === "expired"}
              >
                {copied ? "✓ Copied" : <><ClipboardIcon width={14} height={14} /> Copy</>}
              </button>
            </div>
          </div>
        )}

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
              <AlertTriangleIcon width={16} height={16} /> Token expired. Ask me to generate a new one.
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
              {showRawOutput ? <><ChevronDownIcon width={14} height={14} /> Hide raw output</> : <><ChevronRightIcon width={14} height={14} /> Show raw output</>}
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
        longhouse_url: parsed.data.longhouse_url,
        docker_command: parsed.data.docker_command,
        one_liner_install_command: parsed.data.one_liner_install_command || "",
      };
    }
  } catch {
    // Not JSON or invalid format
  }
  return null;
}
