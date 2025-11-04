import { ThreadMessage } from "../../services/api";

interface ToolMessageProps {
  message: ThreadMessage;
}

export function ToolMessage({ message }: ToolMessageProps) {
  const toolName = message.tool_name || "tool";
  const toolCallId = message.tool_call_id || "";

  return (
    <details className="disclosure" data-tool-call-id={toolCallId}>
      <summary className="disclosure__summary">
        🛠️ Tool Used: {toolName}
      </summary>
      <div className="disclosure__content">
        <div>
          <div className="tool-detail-row">
            <strong>Tool:</strong> {toolName}
          </div>
          {message.name && (
            <div className="tool-detail-row">
              <strong>Inputs:</strong>
              <pre>{message.name}</pre>
            </div>
          )}
          <div className="tool-detail-row output-row">
            <strong>Output:</strong>
            <pre>{message.content}</pre>
          </div>
        </div>
      </div>
    </details>
  );
}
