/**
 * Custom hook for handling OAuth popup flow.
 * Used by both agent-level and account-level connector pages.
 */

import { useState, useEffect, useCallback } from "react";

interface UseOAuthFlowResult {
  startOAuthFlow: (connectorType: string) => void;
  oauthPending: string | null;
  oauthError: string | null;
  clearError: () => void;
}

export function useOAuthFlow(
  onSuccess: () => void,
  onError?: (error: string) => void
): UseOAuthFlowResult {
  const [oauthPending, setOauthPending] = useState<string | null>(null);
  const [oauthError, setOauthError] = useState<string | null>(null);

  const clearError = useCallback(() => setOauthError(null), []);

  const handleOAuthMessage = useCallback(
    (event: MessageEvent) => {
      if (!event.data || typeof event.data !== "object") return;
      const { success, provider, username, error } = event.data;

      if (provider !== oauthPending) return;

      setOauthPending(null);

      if (success) {
        console.log(`OAuth success: ${provider} connected as ${username || "user"}`);
        onSuccess();
      } else if (error) {
        const errorMsg = `Failed to connect ${provider}: ${error}`;
        console.error(`OAuth failed for ${provider}: ${error}`);
        setOauthError(errorMsg);
        onError?.(errorMsg);
      }
    },
    [oauthPending, onSuccess, onError]
  );

  useEffect(() => {
    window.addEventListener("message", handleOAuthMessage);
    return () => window.removeEventListener("message", handleOAuthMessage);
  }, [handleOAuthMessage]);

  const startOAuthFlow = useCallback((connectorType: string) => {
    setOauthPending(connectorType);
    setOauthError(null);

    const width = 600;
    const height = 700;
    const left = window.screenX + (window.outerWidth - width) / 2;
    const top = window.screenY + (window.outerHeight - height) / 2;

    const popup = window.open(
      `/api/oauth/${connectorType}/authorize`,
      `oauth-${connectorType}`,
      `width=${width},height=${height},left=${left},top=${top},popup=1`
    );

    if (!popup) {
      setOauthPending(null);
      const errorMsg = "Popup blocked. Please allow popups for this site.";
      setOauthError(errorMsg);
      onError?.(errorMsg);
      return;
    }

    const checkClosed = setInterval(() => {
      if (popup.closed) {
        clearInterval(checkClosed);
        setOauthPending(null);
      }
    }, 500);
  }, [onError]);

  return { startOAuthFlow, oauthPending, oauthError, clearError };
}
