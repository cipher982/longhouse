import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectSessionWorkspaceStream, connectTimelineSessionsStream } from "../agents";
import { hostLinkStore } from "@/shared/hostLink/store";

type EventListener = (event: MessageEvent) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  options: EventSourceInit | undefined;
  listeners = new Map<string, EventListener[]>();
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();

  constructor(url: string, options?: EventSourceInit) {
    this.url = url;
    this.options = options;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type: string, payload: unknown) {
    const event = { data: JSON.stringify(payload) } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

describe("Timeline session stream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
    (window as typeof window & { __TEST_WORKER_ID__?: string }).__TEST_WORKER_ID__ = "17";
  });

  afterEach(() => {
    delete (window as typeof window & { __TEST_WORKER_ID__?: string }).__TEST_WORKER_ID__;
    vi.unstubAllGlobals();
  });

  it("connects with cookie auth and routes session upsert events", () => {
    const onSessionUpsert = vi.fn();
    const onTimelineStreamEvent = vi.fn();
    window.addEventListener("longhouse:timeline-stream", onTimelineStreamEvent);
    const disconnect = connectTimelineSessionsStream(
      {
        project: "zerg",
        days_back: 14,
        limit: 50,
        hide_autonomous: false,
      },
      { onSessionUpsert },
      { skipInitialReplay: true },
    );

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toContain("/api/timeline/sessions/stream");
    expect(MockEventSource.instances[0].url).toContain("project=zerg");
    expect(MockEventSource.instances[0].url).toContain("days_back=14");
    expect(MockEventSource.instances[0].url).toContain("limit=50");
    expect(MockEventSource.instances[0].url).toContain("hide_autonomous=false");
    expect(MockEventSource.instances[0].url).toContain("skip_initial_replay=true");
    expect(MockEventSource.instances[0].url).toContain("worker=17");
    expect(MockEventSource.instances[0].options).toEqual({ withCredentials: true });

    MockEventSource.instances[0].emit("connected", { message: "ok" });

    MockEventSource.instances[0].emit("session_upsert", {
      session: {
        thread_id: "session-1",
        head: { id: "session-1" },
        id: "session-1",
        provider: "claude",
        project: "zerg",
        device_id: "device-1",
        environment: "laptop",
        cwd: "/tmp/zerg",
        git_repo: null,
        git_branch: "main",
        started_at: "2026-03-21T12:00:00Z",
        ended_at: null,
        last_activity_at: "2026-03-21T12:04:00Z",
        timeline_anchor_at: "2026-03-21T12:04:00Z",
        user_messages: 2,
        assistant_messages: 2,
        tool_calls: 1,
        summary: "runtime",
        summary_title: "runtime",
        first_user_message: "hello",
        thread_root_session_id: "session-1",
        thread_head_session_id: "session-1",
        thread_continuation_count: 1,
        continued_from_session_id: null,
        continuation_kind: null,
        origin_label: null,
        home_label: null,
        branched_from_event_id: null,
        is_writable_head: true,
      },
      total: 1,
      has_real_sessions: true,
    });

    expect(onSessionUpsert).toHaveBeenCalledTimes(1);
    expect(onSessionUpsert).toHaveBeenCalledWith(
      expect.objectContaining({
        total: 1,
        has_real_sessions: true,
      }),
    );
    const streamEventDetails = onTimelineStreamEvent.mock.calls.map(([event]) => (event as CustomEvent).detail);
    expect(streamEventDetails).toContainEqual(expect.objectContaining({ kind: "connected" }));
    expect(streamEventDetails).toContainEqual(
      expect.objectContaining({ kind: "session_upsert", session_id: "session-1" }),
    );

    disconnect();
    window.removeEventListener("longhouse:timeline-stream", onTimelineStreamEvent);
    expect(MockEventSource.instances[0].close).toHaveBeenCalledTimes(1);
  });

  it("dispatches workspace stream receive metadata for profiler correlation", () => {
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_779_482_800_010);
    const onWorkspaceChanged = vi.fn();
    const onTimelineStreamEvent = vi.fn();
    window.addEventListener("longhouse:timeline-stream", onTimelineStreamEvent);

    const disconnect = connectSessionWorkspaceStream(
      "session-1",
      { onWorkspaceChanged },
      { skipInitial: true, knownWorkspaceFingerprint: "sha256:cached" },
    );

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toContain(
      "/api/timeline/sessions/session-1/workspace/stream",
    );
    expect(MockEventSource.instances[0].url).toContain("skip_initial=true");
    expect(MockEventSource.instances[0].url).toContain("known_workspace_fingerprint=sha256%3Acached");

    MockEventSource.instances[0].emit("connected", {
      session_id: "session-1",
      server_now_ms: 1_779_482_799_950,
    });
    MockEventSource.instances[0].emit("workspace_changed", {
      session_id: "session-1",
      change_kind: "transcript_preview",
      latest_event_id: 42,
      latest_event_emitted_at_ms: 1_779_482_799_900,
      server_fanout_at_ms: 1_779_482_799_980,
      server_now_ms: 1_779_482_800_000,
      catalog_commit_seq: 41,
      pubsub_seq: 7,
      transcript_preview: {
        event_id: 42,
        text: "hello profiler",
        event_origin: "live_provisional",
      },
    });

    expect(onWorkspaceChanged).toHaveBeenCalledWith(
      expect.objectContaining({
        session_id: "session-1",
        latest_event_id: 42,
        catalog_commit_seq: 41,
      }),
    );
    const streamEventDetails = onTimelineStreamEvent.mock.calls.map(([event]) => (event as CustomEvent).detail);
    expect(streamEventDetails).toContainEqual(
      expect.objectContaining({
        kind: "workspace_connected",
        session_id: "session-1",
        server_now_ms: 1_779_482_799_950,
        client_received_at_ms: 1_779_482_800_010,
      }),
    );
    expect(streamEventDetails).toContainEqual(
      expect.objectContaining({
        kind: "workspace_changed",
        session_id: "session-1",
        change_kind: "transcript_preview",
        latest_event_id: 42,
        latest_event_emitted_at_ms: 1_779_482_799_900,
        server_fanout_at_ms: 1_779_482_799_980,
        server_now_ms: 1_779_482_800_000,
        pubsub_seq: 7,
        client_received_at_ms: 1_779_482_800_010,
        has_transcript_preview: true,
        transcript_preview_event_id: 42,
        transcript_preview_origin: "live_provisional",
        transcript_preview_text_length: 14,
      }),
    );

    disconnect();
    window.removeEventListener("longhouse:timeline-stream", onTimelineStreamEvent);
    nowSpy.mockRestore();
  });

  it("routes replay gaps to the workspace stream handler", () => {
    const onReplayGap = vi.fn();
    const disconnect = connectSessionWorkspaceStream("session-1", { onReplayGap });

    MockEventSource.instances[0].emit("replay_gap", {
      session_id: "session-1",
      requested_seq: 3,
      earliest_seq: 8,
      latest_seq: 12,
      reason: "cursor_too_old",
    });

    expect(onReplayGap).toHaveBeenCalledWith({
      session_id: "session-1",
      requested_seq: 3,
      earliest_seq: 8,
      latest_seq: 12,
      reason: "cursor_too_old",
    });

    disconnect();
  });
  it("feeds connected runtime fields and lifecycle events into the shared store", () => {
    const disconnectTimeline = connectTimelineSessionsStream();
    MockEventSource.instances[0].emit("connected", {
      stream_epoch: "stream-1",
      runtime_epoch: "runtime-candidate",
      admission: "draining",
    });
    MockEventSource.instances[0].emit("host_lifecycle", {
      type: "host.lifecycle",
      state: "updating",
      runtime_epoch: "runtime-candidate",
      attempt_id: "attempt-1",
      phase: "drain",
      expected_back_by: new Date(Date.now() + 30_000).toISOString(),
      deadline: new Date(Date.now() + 60_000).toISOString(),
      cutoff: new Date(Date.now() + 90_000).toISOString(),
    });

    expect(hostLinkStore.getSnapshot()).toMatchObject({
      state: "updating",
      runtimeEpoch: "runtime-candidate",
      claim: { attempt_id: "attempt-1" },
    });
    disconnectTimeline();

    const disconnectWorkspace = connectSessionWorkspaceStream("session-1");
    MockEventSource.instances[1].emit("connected", {
      session_id: "session-1",
      runtime_epoch: "runtime-candidate",
      admission: "open",
    });
    expect(hostLinkStore.getSnapshot()).toMatchObject({
      state: "serving",
      runtimeEpoch: "runtime-candidate",
      claim: null,
    });
    disconnectWorkspace();
  });
});
