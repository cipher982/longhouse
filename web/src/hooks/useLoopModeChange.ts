import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { setSessionLoopMode, type SessionLoopMode } from "../services/api/agents";
import type { AgentSession, AgentSessionThreadResponse, AgentSessionWorkspaceResponse } from "../services/api";

/**
 * Manages optimistic loop mode changes with cache invalidation.
 * Returns the effective loop mode (with optimistic override) and a change handler.
 */
export function useLoopModeChange(session: AgentSession | null) {
  const queryClient = useQueryClient();
  const [loopModeOverride, setLoopModeOverride] = useState<SessionLoopMode | null>(null);
  const [loopModePending, setLoopModePending] = useState(false);

  const effectiveLoopMode = loopModeOverride ?? session?.loop_mode ?? "manual";

  const handleLoopModeChange = async (nextMode: SessionLoopMode) => {
    if (!session || loopModePending || nextMode === effectiveLoopMode) {
      return;
    }
    setLoopModeOverride(nextMode);
    setLoopModePending(true);
    try {
      const updatedSession = await setSessionLoopMode(session.id, nextMode);
      queryClient.setQueryData<AgentSession>(["agent-session", session.id], (current) =>
        current ? { ...current, loop_mode: updatedSession.loop_mode } : current,
      );
      queryClient.setQueryData<AgentSessionThreadResponse>(["agent-session-thread", session.id], (current) => {
        if (!current) {
          return current;
        }
        return {
          ...current,
          sessions: current.sessions.map((threadSession) =>
            threadSession.id === session.id
              ? { ...threadSession, loop_mode: updatedSession.loop_mode }
              : threadSession,
          ),
        };
      });
      queryClient.setQueriesData<AgentSessionWorkspaceResponse>(
        { queryKey: ["agent-session-workspace", session.id] },
        (current) => {
          if (!current) {
            return current;
          }
          return {
            ...current,
            session:
              current.session.id === session.id
                ? { ...current.session, loop_mode: updatedSession.loop_mode }
                : current.session,
            thread: {
              ...current.thread,
              sessions: current.thread.sessions.map((threadSession) =>
                threadSession.id === session.id
                  ? { ...threadSession, loop_mode: updatedSession.loop_mode }
                  : threadSession,
              ),
            },
          };
        },
      );
      setLoopModeOverride(null);
      toast.success(`Loop mode set to ${nextMode}.`);
    } catch (error) {
      setLoopModeOverride(null);
      toast.error(error instanceof Error ? error.message : "Failed to update loop mode.");
    } finally {
      setLoopModePending(false);
    }
  };

  return { effectiveLoopMode, loopModePending, handleLoopModeChange };
}
