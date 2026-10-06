import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/features/auth/auth";
import {
  startTranscriptPersistence,
  type TranscriptPersistence as Persistence,
} from "@/features/session/transcriptPersistence";
import { setTranscriptCacheUser, wipeTranscriptCache } from "@/shared/api/transcriptCache";

/**
 * Keeps recently viewed transcripts on this device (session-view-terminal-parity
 * C5). Scoped to the signed-in user; an explicit sign-out wipes it.
 */
export function TranscriptPersistence() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const persistenceRef = useRef<Persistence | null>(null);

  useEffect(() => {
    const persistence = startTranscriptPersistence(queryClient);
    persistenceRef.current = persistence;
    const onLogout = () => void wipeTranscriptCache();
    window.addEventListener("longhouse-auth-logout", onLogout);
    return () => {
      window.removeEventListener("longhouse-auth-logout", onLogout);
      persistence.stop();
      persistenceRef.current = null;
    };
  }, [queryClient]);

  const userId = user?.id ?? null;
  useEffect(() => {
    let cancelled = false;
    void setTranscriptCacheUser(userId).then(() => {
      if (!cancelled && userId != null) void persistenceRef.current?.paintRecentSessions();
    });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  return null;
}
