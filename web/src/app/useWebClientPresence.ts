import { useEffect, useMemo } from "react";
import { useLocation } from "react-router";
import config from "@/shared/lib/config";
import { postWebClientPresence } from "@/shared/api/clientPresence";
import { useDocumentVisible } from "@/shared/hooks/useDocumentVisible";
import { getWindowActivity } from "@/shared/hooks/useWindowActivity";
import { NEW_SESSION_PATH } from "@/features/launch/newSessionPath";

const WEB_CLIENT_ID_STORAGE_KEY = "longhouse.webClientId";
const WEB_CLIENT_HEARTBEAT_MS = 30_000;
const WEB_CLIENT_AMBIENT_HEARTBEAT_MS = 120_000;

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
  // The new-session pane shares the session URL shape but shows no session.
  if (!match || pathname.replace(/\/$/, "") === NEW_SESSION_PATH) {
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
    let timeoutId: number | null = null;
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

    const scheduleNext = () => {
      if (cancelled || !documentVisible) return;
      const delay =
        getWindowActivity() === "ambient"
          ? WEB_CLIENT_AMBIENT_HEARTBEAT_MS
          : WEB_CLIENT_HEARTBEAT_MS;
      timeoutId = window.setTimeout(() => {
        timeoutId = null;
        if (cancelled) return;
        sendHeartbeat();
        scheduleNext();
      }, delay);
    };

    sendHeartbeat();
    scheduleNext();
    return () => {
      cancelled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [clientId, documentVisible, enabled, route, sessionId]);
}
