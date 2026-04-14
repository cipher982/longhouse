import { useEffect, useRef, useState } from "react";

import type { ManagedLaunchSuggestion } from "../../lib/sessionWorkspace";
import { Button } from "../ui";
import "../../styles/managed-launch-hint.css";

interface ManagedLaunchHintCardProps {
  suggestion: ManagedLaunchSuggestion;
  testId?: string;
}

export function ManagedLaunchHintCard({ suggestion, testId }: ManagedLaunchHintCardProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (resetTimerRef.current) {
        clearTimeout(resetTimerRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    setCopyError(null);
    try {
      const clipboard = globalThis.navigator?.clipboard;
      if (!clipboard?.writeText) {
        throw new Error("Clipboard API unavailable");
      }
      await clipboard.writeText(suggestion.command);
      setCopied(true);
      if (resetTimerRef.current) {
        clearTimeout(resetTimerRef.current);
      }
      resetTimerRef.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
      setCopyError("Copy failed. Use the command manually.");
    }
  };

  return (
    <div className="managed-launch-hint" data-testid={testId}>
      <div className="managed-launch-hint__copy">
        <div className="managed-launch-hint__title">{suggestion.title}</div>
        <div className="managed-launch-hint__body">{suggestion.body}</div>
      </div>
      <div className="managed-launch-hint__command-row">
        <code
          className="managed-launch-hint__command"
          data-testid={testId ? `${testId}-command` : undefined}
        >
          {suggestion.command}
        </code>
        <Button
          type="button"
          variant="tertiary"
          size="sm"
          onClick={handleCopy}
          className="managed-launch-hint__copy-button"
          aria-label={`Copy command: ${suggestion.command}`}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {copyError ? <div className="managed-launch-hint__meta">{copyError}</div> : null}
    </div>
  );
}
