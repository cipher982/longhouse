import { useEffect, useMemo } from "react";
import { useLocation } from "react-router-dom";
import config from "../lib/config";
import { postWebClientPresence } from "../services/api/clientPresence";
import { useDocumentVisible } from "./useDocumentVisible";

const WEB_CLIENT_ID_STORAGE_KEY = "longhouse.webClientId";
const WEB_CLIENT_HEARTBEAT_MS = 30_000;

function randomClientId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export function getOrCreateWebClientId(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const existing = window.localStorage.getItem(WEB_CLIENT_ID_STORAGE_KEY);
    if (existing) {
      return existing;
    }
    const next = randomClientId();
    window.localStorage.setItem(WEB_CLIENT_ID_STORAGE_KEY, next);
    return next;
  } catch {
    return randomClientId();
  }
}

function extractTimelineSessionId(pathname: string): string | null {
  const match = pathname.match(/^\/timeline\/([^/?#]+)/);
  if (!match) {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function useWebClientPresence(): void {
  const location = useLocation();
  const documentVisible = useDocumentVisible();
  const enabled = config.authEnabled && !config.demoMode;
  const clientId = useMemo(() => (enabled ? getOrCreateWebClientId() : null), [enabled]);
  const route = `${location.pathname}${location.search}`.slice(0, 512);
  const sessionId = extractTimelineSessionId(location.pathname);

  useEffect(() => {
    if (!enabled || !clientId) {
      return;
    }

    let cancelled = false;
    const sendHeartbeat = () => {
      if (cancelled) {
        return;
      }
      void postWebClientPresence({
        client_id: clientId,
        client_type: "web",
        visible: documentVisible,
        route,
        session_id: sessionId,
      }).catch(() => {
        // Presence is advisory. Auth refresh and API health cover actionable errors elsewhere.
      });
    };

    sendHeartbeat();
    if (!documentVisible) {
      return () => {
        cancelled = true;
      };
    }

    const intervalId = window.setInterval(sendHeartbeat, WEB_CLIENT_HEARTBEAT_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [clientId, documentVisible, enabled, route, sessionId]);
}
