import { useState } from "react";
import clsx from "clsx";
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { ThreadMessage } from "../../services/api";

interface ToolMessageProps {
  message: ThreadMessage;
}

export function ToolMessage({ message }: ToolMessageProps) {
  const [isOpen, setIsOpen] = useState(false);
  const toolName = message.tool_name || "tool";
  const toolCallId = message.tool_call_id || "";
  
  // Determine status based on content (if content is empty, it might be processing)
  const isProcessing = !message.content && !message.name; // simplistic check
  
  const toggleOpen = () => setIsOpen(!isOpen);

  return (
    <div className="tool-message-container" data-tool-call-id={toolCallId}>
      <div 
        className={clsx("tool-summary", { "is-open": isOpen })}
        onClick={toggleOpen}
      >
        <div className="tool-icon">🛠️</div>
        <span className="tool-name">Used <strong>{toolName}</strong></span>
        <span className="tool-status-indicator">
           {isProcessing ? "Running..." : "Completed"}
        </span>
        <div className={clsx("chevron", { "open": isOpen })}>▼</div>
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
