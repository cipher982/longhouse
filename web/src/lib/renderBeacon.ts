/**
 * Client-side realtime latency beacon.
 *
 * Measures provider-emitted → browser-rendered latency for SSE-delivered
 * events and posts to /api/telemetry/client-render. The beacon is fire-
 * and-forget; we never retry, and failures are swallowed.
 *
 * Clock skew: the SSE `connected` frame carries server_now_ms. We compute
 * (client_now - server_now) at connect time and send that with each beacon
 * so the server can correct for skewed client clocks.
 */

import { buildUrl } from "../services/api/base";

interface BeaconPayload {
  event_id: string;
  session_id: string | null;
  surface: "web";
  render_kind?: "event" | "state";
  managed: boolean;
  emitted_at_ms: number;
  rendered_at_ms: number;
  clock_skew_ms: number;
  clock_sync_rtt_ms?: number | null;
  clock_sync_uncertainty_ms?: number | null;
  clock_sync_sample_count?: number | null;
  server_fanout_at_ms?: number | null;
  client_received_at_ms?: number | null;
  pubsub_seq?: number | null;
  state_commit_seq?: number | null;
  state_phase?: string | null;
  state_observed_at_ms?: number | null;
}

let _skewMs = 0;
let _clockSyncRttMs: number | null = null;
let _clockSyncUncertaintyMs: number | null = null;
let _clockSyncSampleCount = 0;
let _clockCalibrationPromise: Promise<void> | null = null;
let _lastBeaconedEventKey: string | null = null;

export function recordServerClockSkew(serverNowMs: number | undefined): void {
  if (typeof serverNowMs !== "number" || !Number.isFinite(serverNowMs)) return;
  if (_clockSyncRttMs != null) return;
  _skewMs = Date.now() - serverNowMs;
  void calibrateServerClock();
}

export function getClockSkewMs(): number {
  return _skewMs;
}

export function getClockCalibration(): {
  clockSkewMs: number;
  rttMs: number | null;
  uncertaintyMs: number | null;
  sampleCount: number;
} {
  return {
    clockSkewMs: _skewMs,
    rttMs: _clockSyncRttMs,
    uncertaintyMs: _clockSyncUncertaintyMs,
    sampleCount: _clockSyncSampleCount,
  };
}

export function resetClockCalibrationForTests(): void {
  _skewMs = 0;
  _clockSyncRttMs = null;
  _clockSyncUncertaintyMs = null;
  _clockSyncSampleCount = 0;
  _clockCalibrationPromise = null;
}

export function recordClockSyncSample(sample: {
  clientSentAtMs: number;
  serverReceivedAtMs: number;
  serverSentAtMs: number;
  clientReceivedAtMs: number;
}): void {
  const { clientSentAtMs: t0, serverReceivedAtMs: t1, serverSentAtMs: t2, clientReceivedAtMs: t3 } = sample;
  if (![t0, t1, t2, t3].every(Number.isFinite) || t3 < t0 || t2 < t1) return;

  const rttMs = Math.max(0, Math.round(t3 - t0 - (t2 - t1)));
  const clientAheadMs = Math.round(((t0 - t1) + (t3 - t2)) / 2);
  _clockSyncSampleCount += 1;
  if (_clockSyncRttMs == null || rttMs < _clockSyncRttMs) {
    _clockSyncRttMs = rttMs;
    _clockSyncUncertaintyMs = Math.ceil(rttMs / 2);
    _skewMs = clientAheadMs;
  }
}

export function calibrateServerClock(rounds = 5): Promise<void> {
  if (_clockCalibrationPromise) return _clockCalibrationPromise;
  _clockCalibrationPromise = (async () => {
    for (let index = 0; index < rounds; index += 1) {
      const clientSentAtMs = Date.now();
      try {
        const response = await fetch(buildUrl("/telemetry/clock"), { cache: "no-store" });
        const clientReceivedAtMs = Date.now();
        if (!response.ok) continue;
        const payload = (await response.json()) as {
          server_received_at_ms?: number;
          server_sent_at_ms?: number;
        };
        if (payload.server_received_at_ms == null || payload.server_sent_at_ms == null) continue;
        recordClockSyncSample({
          clientSentAtMs,
          serverReceivedAtMs: payload.server_received_at_ms,
          serverSentAtMs: payload.server_sent_at_ms,
          clientReceivedAtMs,
        });
      } catch {
        // The SSE timestamp remains a coarse fallback when calibration fails.
      }
    }
  })().finally(() => {
    _clockCalibrationPromise = null;
  });
  return _clockCalibrationPromise;
}

/**
 * Emit a render beacon for the latest workspace event. Scheduled via rAF so
 * we measure after the browser actually paints the new state, not just after
 * React re-renders.
 *
 * Idempotent per event_id: repeated calls with the same event_id are ignored.
 */
export function emitRenderBeacon(params: {
  sessionId: string;
  latestEventId: string | number;
  latestEventEmittedAtMs: number | null | undefined;
  managed: boolean;
  serverFanoutAtMs?: number | null;
  clientReceivedAtMs?: number | null;
  pubsubSeq?: number | null;
  stateCommitSeq?: number | null;
  statePhase?: string | null;
  stateObservedAtMs?: number | null;
}): void {
  if (typeof window === "undefined") return;
  if (!params.latestEventEmittedAtMs) return;
  const beaconKey = `${params.sessionId}:${params.latestEventId}`;
  if (beaconKey === _lastBeaconedEventKey) return;
  _lastBeaconedEventKey = beaconKey;

  const send = () => {
    const payload: BeaconPayload = {
      event_id: String(params.latestEventId),
      session_id: params.sessionId,
      surface: "web",
      render_kind: "event",
      managed: params.managed,
      emitted_at_ms: params.latestEventEmittedAtMs!,
      rendered_at_ms: Date.now(),
      clock_skew_ms: _skewMs,
      clock_sync_rtt_ms: _clockSyncRttMs,
      clock_sync_uncertainty_ms: _clockSyncUncertaintyMs,
      clock_sync_sample_count: _clockSyncSampleCount,
      server_fanout_at_ms: params.serverFanoutAtMs ?? null,
      client_received_at_ms: params.clientReceivedAtMs ?? null,
      pubsub_seq: params.pubsubSeq ?? null,
      state_commit_seq: params.stateCommitSeq ?? null,
      state_phase: params.statePhase ?? null,
      state_observed_at_ms: params.stateObservedAtMs ?? null,
    };

    try {
      const url = buildUrl("/telemetry/client-render");
      const body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
      } else {
        void fetch(url, { method: "POST", body, headers: { "Content-Type": "application/json" } });
      }
    } catch {
      // Beacon is best-effort.
    }
  };

  // rAF + setTimeout(0) gets us past layout + paint in most browsers.
  window.requestAnimationFrame(() => window.setTimeout(send, 0));
}

/**
 * Emit a beacon for a canonical runtime-state commit even when no durable
 * transcript event accompanied the wake. The server fanout timestamp is the
 * state-lane emission coordinate; the normal client clock correction applies.
 */
export function emitStateRenderBeacon(params: {
  sessionId: string;
  catalogCommitSeq: number;
  statePhase?: string | null;
  stateObservedAtMs?: number | null;
  managed: boolean;
  serverFanoutAtMs?: number | null;
  clientReceivedAtMs?: number | null;
  pubsubSeq?: number | null;
}): void {
  if (typeof window === "undefined") return;
  if (!Number.isFinite(params.catalogCommitSeq) || params.catalogCommitSeq <= 0) return;
  if (!params.serverFanoutAtMs) return;

  const beaconKey = `${params.sessionId}:state:${params.catalogCommitSeq}`;
  if (beaconKey === _lastBeaconedEventKey) return;
  _lastBeaconedEventKey = beaconKey;

  const send = () => {
    const payload: BeaconPayload = {
      event_id: `state:${params.catalogCommitSeq}`,
      session_id: params.sessionId,
      surface: "web",
      render_kind: "state",
      managed: params.managed,
      emitted_at_ms: params.serverFanoutAtMs!,
      rendered_at_ms: Date.now(),
      clock_skew_ms: _skewMs,
      clock_sync_rtt_ms: _clockSyncRttMs,
      clock_sync_uncertainty_ms: _clockSyncUncertaintyMs,
      clock_sync_sample_count: _clockSyncSampleCount,
      server_fanout_at_ms: params.serverFanoutAtMs,
      client_received_at_ms: params.clientReceivedAtMs ?? null,
      pubsub_seq: params.pubsubSeq ?? null,
      state_commit_seq: params.catalogCommitSeq,
      state_phase: params.statePhase ?? null,
      state_observed_at_ms: params.stateObservedAtMs ?? null,
    };

    try {
      const url = buildUrl("/telemetry/client-render");
      const body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
      } else {
        void fetch(url, { method: "POST", body, headers: { "Content-Type": "application/json" } });
      }
    } catch {
      // Beacon is best-effort.
    }
  };

  window.requestAnimationFrame(() => window.setTimeout(send, 0));
}
