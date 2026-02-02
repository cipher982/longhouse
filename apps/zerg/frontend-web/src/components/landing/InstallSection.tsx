import { useState } from "react";

type Platform = "macos" | "linux" | "windows";

const installCommands: Record<Platform, { label: string; command: string; note?: string }> = {
  macos: {
    label: "macOS",
    command: "curl -fsSL https://longhouse.ai/install.sh | bash",
  },
  linux: {
    label: "Linux",
    command: "curl -fsSL https://longhouse.ai/install.sh | bash",
  },
  windows: {
    label: "Windows (WSL)",
    command: "curl -fsSL https://longhouse.ai/install.sh | bash",
    note: "Run in WSL terminal. Native Windows coming soon.",
  },
};

interface InstallSectionProps {
  className?: string;
}

export function InstallSection({ className = "" }: InstallSectionProps) {
  const [platform, setPlatform] = useState<Platform>("macos");
  const [copied, setCopied] = useState(false);

  const currentCommand = installCommands[platform];

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(currentCommand.command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers
      const textArea = document.createElement("textarea");
      textArea.value = currentCommand.command;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      document.body.removeChild(textArea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className={`install-section ${className}`}>
      <div className="install-header">
        <h3>Get started in seconds</h3>
        <p>One command. No dependencies.</p>
      </div>

      <div className="install-tabs" role="tablist" aria-label="Installation platform">
        {(Object.keys(installCommands) as Platform[]).map((p) => (
          <button
            key={p}
            role="tab"
            aria-selected={platform === p}
            className={`install-tab ${platform === p ? "active" : ""}`}
            onClick={() => setPlatform(p)}
          >
            {installCommands[p].label}
          </button>
        ))}
      </div>

      <div className="install-command-container">
        <button
          type="button"
          className="install-command"
          onClick={handleCopy}
          aria-label={`Copy install command: ${currentCommand.command}`}
        >
          <span className="install-prompt" aria-hidden="true">$</span>
          <code className="install-text">{currentCommand.command}</code>
        </button>
        <button
          type="button"
          className={`install-copy-btn ${copied ? "copied" : ""}`}
          onClick={handleCopy}
          aria-label={copied ? "Copied!" : "Copy to clipboard"}
        >
          {copied ? (
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
              <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
              <path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25v-7.5z" />
              <path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25v-7.5zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25h-7.5z" />
            </svg>
          )}
        </button>
      </div>

      {currentCommand.note && (
        <p className="install-note">{currentCommand.note}</p>
      )}

      <div className="install-features">
        <span className="install-feature">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
          </svg>
          No sudo required
        </span>
        <span className="install-feature">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
          </svg>
          Works offline
        </span>
        <span className="install-feature">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
          </svg>
          &lt;2 min setup
        </span>
      </div>
    </div>
  );
}
