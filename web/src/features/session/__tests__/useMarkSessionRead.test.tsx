import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useMarkSessionRead } from "../useMarkSessionRead";
import type {
  AgentSessionWorkspaceResponse,
  SessionStateFacts,
  TimelineSessionsListResponse,
} from "@/shared/api/agents";
import * as agentsApi from "@/shared/api/agents";

function makeStateFacts(overrides: Partial<SessionStateFacts> = {}): SessionStateFacts {
  return {
    mode: "console",
    disposition: { state: "open", reason: null },
    launch: { state: "launched", reason: null },
    run: { lifecycle: "ended", end_reason: "completed" },
    activity: { state: "quiescent", raw_kind: null, observed_at: null, expires_at: null },
    delegation: { state: "none", count: 0, kinds: {}, items: [] },
    control: {
      capability: "live_control",
      terminal_attached: false,
      owner_connected: true,
      source_runner_name: "cube-ws",
    },
    pending_interaction: null,
    transcript: { convergence: "converged", last_append_at: null },
    host: { connectivity: "healthy", reason: null },
    working_set: "history",
    unread: true,
    last_result_at: "2026-11-25T15:19:19.455354Z",
    last_result_outcome: "completed",
    last_user_input_at: null,
    presentation: { primary: null, signal: "idle", access: null, transcript: null },
    commit_seq: 1,
    ...overrides,
  } as SessionStateFacts;
}

function makeListResponse(sessionId: string, facts: SessionStateFacts): TimelineSessionsListResponse {
  return {
    sessions: [
      {
        thread_id: sessionId,
        head: {
          id: sessionId,
          session_state: facts,
        },
      },
    ],
    total: 1,
    has_real_sessions: true,
  } as unknown as TimelineSessionsListResponse;
}

function makeWorkspaceResponse(sessionId: string, facts: SessionStateFacts): AgentSessionWorkspaceResponse {
  return {
    session: {
      id: sessionId,
      session_state: facts,
    },
  } as unknown as AgentSessionWorkspaceResponse;
}

function Harness({
  sessionId,
  sessionState,
}: {
  sessionId: string | null;
  sessionState: SessionStateFacts | null | undefined;
}) {
  useMarkSessionRead({ sessionId, sessionState });
  return null;
}

function setVisibilityState(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
}

describe("useMarkSessionRead", () => {
  beforeEach(() => {
    setVisibilityState("visible");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    setVisibilityState("visible");
  });

  it("clears unread in cached session lists and workspace immediately after markSessionRead resolves", async () => {
    const sessionId = "4cf77e87-b16e-4aa2-8306-180bcd12aac1";
    const readThrough = "2026-11-25T15:19:19.455354Z";
    const facts = makeStateFacts({ unread: true, last_result_at: readThrough });

    const markSpy = vi
      .spyOn(agentsApi, "markSessionRead")
      .mockResolvedValue({ session_id: sessionId, last_read_at: readThrough });

    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    queryClient.setQueryData(["agent-sessions", { limit: 60 }], makeListResponse(sessionId, facts));
    queryClient.setQueryData(
      ["agent-session-workspace", sessionId, { limit: 200, branch_mode: "head" }],
      makeWorkspaceResponse(sessionId, facts),
    );

    render(
      <QueryClientProvider client={queryClient}>
        <Harness sessionId={sessionId} sessionState={facts} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(markSpy).toHaveBeenCalledTimes(1);
      expect(markSpy).toHaveBeenCalledWith(sessionId, readThrough);
    });

    await waitFor(() => {
      const list = queryClient.getQueryData<TimelineSessionsListResponse>(["agent-sessions", { limit: 60 }]);
      expect(list?.sessions[0].head.session_state.unread).toBe(false);
    });

    const workspace = queryClient.getQueryData<AgentSessionWorkspaceResponse>([
      "agent-session-workspace",
      sessionId,
      { limit: 200, branch_mode: "head" },
    ]);
    expect(workspace?.session.session_state.unread).toBe(false);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agent-sessions"] });
  });

  it("does not clear a newer unread result in cache than the acknowledged readThrough", async () => {
    const sessionId = "session-race";
    const olderResult = "2026-11-25T15:19:19Z";
    const newerResult = "2026-11-25T15:25:00Z";

    vi.spyOn(agentsApi, "markSessionRead").mockResolvedValue({
      session_id: sessionId,
      last_read_at: olderResult,
    });

    const queryClient = new QueryClient();
    queryClient.setQueryData(
      ["agent-sessions", { limit: 60 }],
      makeListResponse(sessionId, makeStateFacts({ unread: true, last_result_at: newerResult })),
    );

    render(
      <QueryClientProvider client={queryClient}>
        <Harness
          sessionId={sessionId}
          sessionState={makeStateFacts({ unread: true, last_result_at: olderResult })}
        />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(agentsApi.markSessionRead).toHaveBeenCalledTimes(1);
    });

    const list = queryClient.getQueryData<TimelineSessionsListResponse>(["agent-sessions", { limit: 60 }]);
    expect(list?.sessions[0].head.session_state.unread).toBe(true);
  });

  it("waits until the tab becomes visible before marking read", async () => {
    setVisibilityState("hidden");
    const sessionId = "session-hidden";
    const readThrough = "2026-11-25T15:19:19Z";
    const markSpy = vi
      .spyOn(agentsApi, "markSessionRead")
      .mockResolvedValue({ session_id: sessionId, last_read_at: readThrough });

    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <Harness
          sessionId={sessionId}
          sessionState={makeStateFacts({ unread: true, last_result_at: readThrough })}
        />
      </QueryClientProvider>,
    );

    expect(markSpy).not.toHaveBeenCalled();

    setVisibilityState("visible");
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => {
      expect(markSpy).toHaveBeenCalledTimes(1);
      expect(markSpy).toHaveBeenCalledWith(sessionId, readThrough);
    });
  });

  it("clears the dedup key after failure so a later unread observation can retry", async () => {
    const sessionId = "session-dedup";
    const readThrough = "2026-11-25T15:19:19Z";
    const markSpy = vi
      .spyOn(agentsApi, "markSessionRead")
      .mockRejectedValueOnce(new Error("transient 502"))
      .mockResolvedValueOnce({ session_id: sessionId, last_read_at: readThrough });

    const queryClient = new QueryClient();
    const facts = makeStateFacts({ unread: true, last_result_at: readThrough });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <Harness sessionId={sessionId} sessionState={facts} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(markSpy).toHaveBeenCalledTimes(1);
    });

    // After the failed attempt clears ackedKeyRef, toggling unread back on retries cleanly.
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <Harness sessionId={sessionId} sessionState={makeStateFacts({ unread: false, last_result_at: readThrough })} />
      </QueryClientProvider>,
    );
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <Harness sessionId={sessionId} sessionState={makeStateFacts({ unread: true, last_result_at: readThrough })} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(markSpy).toHaveBeenCalledTimes(2);
    });
  });
});
