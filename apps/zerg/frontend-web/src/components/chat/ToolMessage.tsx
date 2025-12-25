import { useState, useMemo } from "react";
import clsx from "clsx";
import { SyntaxHighlighter, oneDark } from '../../lib/syntaxHighlighter';
import { ThreadMessage } from "../../services/api";
import { RunnerSetupCard, parseRunnerSetupData } from "./RunnerSetupCard";
import { WrenchIcon } from "../icons";

interface ToolMessageProps {
  message: ThreadMessage;
}

export function ToolMessage({ message }: ToolMessageProps) {
  const [isOpen, setIsOpen] = useState(false);
  const toolName = message.tool_name || "tool";
  const toolCallId = message.tool_call_id || "";

  // Check if this tool result should render as a special card
  const runnerSetupData = useMemo(() => {
    if (toolName === "runner_create_enroll_token" && message.content) {
      return parseRunnerSetupData(message.content);
    }
    return null;
  }, [toolName, message.content]);

  // Render RunnerSetupCard for runner enrollment tool
  if (runnerSetupData) {
    return (
      <div className="tool-message-container tool-message-card" data-tool-call-id={toolCallId}>
        <RunnerSetupCard data={runnerSetupData} rawContent={message.content} />
      </div>
    );
  }

  // Determine status based on content (if content is empty, it might be processing)
  const isProcessing = !message.content && !message.name; // simplistic check

  const toggleOpen = () => setIsOpen(!isOpen);

  return (
    <div className="tool-message-container" data-tool-call-id={toolCallId}>
      <div
        className={clsx("tool-summary", { "is-open": isOpen })}
        onClick={toggleOpen}
      >
        <div className="tool-icon"><WrenchIcon width={16} height={16} /></div>
        <span className="tool-name">Used <strong>{toolName}</strong></span>
        <span className="tool-status-indicator">
           {isProcessing ? "Running..." : "Completed"}
        </span>
        <div className={clsx("chevron", { "open": isOpen })}>▶</div>
      </div>

      {isOpen && (
        <div className="tool-details">
          {message.name && (
            <div className="tool-section">
              <div className="tool-section-header">Input</div>
              <div className="tool-code-block">
                <SyntaxHighlighter
                   language="json"
                   style={oneDark}
                   customStyle={{ margin: 0, borderRadius: '4px', fontSize: '12px' }}
                   wrapLongLines={true}
                >
                   {message.name}
                </SyntaxHighlighter>
              </div>
            </div>
          )}

          <div className="tool-section">
            <div className="tool-section-header">Output</div>
             <div className="tool-code-block">
                <SyntaxHighlighter
                   language="json"
                   style={oneDark}
                   customStyle={{ margin: 0, borderRadius: '4px', fontSize: '12px' }}
                   wrapLongLines={true}
                >
                   {message.content || "(No output)"}
                </SyntaxHighlighter>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
