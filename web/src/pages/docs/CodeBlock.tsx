import { useState } from "react";
import { trackAcquisitionEvent } from "../../lib/analytics";

interface CodeBlockProps {
  children: string;
  title?: string;
}

export function CodeBlock({ children, title }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      const command = children.trim();
      await navigator.clipboard.writeText(command);
      if (command.includes("get.longhouse.ai/install.sh") || command.includes("longhouse connect --install")) {
        trackAcquisitionEvent("docs_command_copy", {
          surface: "docs",
          command: command.includes("get.longhouse.ai/install.sh") ? "install_sh" : "connect_install",
          title: title ?? null,
        });
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard not available */
    }
  };

  return (
    <div className="docs-codeblock">
      <div className="docs-codeblock-header">
        {title && <span className="docs-codeblock-title">{title}</span>}
        <button
          className="docs-codeblock-copy"
          onClick={handleCopy}
          aria-label="Copy to clipboard"
        >
          {copied ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          )}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre><code>{children.trim()}</code></pre>
    </div>
  );
}
