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
  managed: boolean;
  emitted_at_ms: number;
  rendered_at_ms: number;
  clock_skew_ms: number;
  server_fanout_at_ms?: number | null;
  client_received_at_ms?: number | null;
  pubsub_seq?: number | null;
}

let _skewMs = 0;
let _lastBeaconedEventKey: string | null = null;

export function recordServerClockSkew(serverNowMs: number | undefined): void {
  if (typeof serverNowMs !== "number" || !Number.isFinite(serverNowMs)) return;
  _skewMs = Date.now() - serverNowMs;
}

export function getClockSkewMs(): number {
  return _skewMs;
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
      managed: params.managed,
      emitted_at_ms: params.latestEventEmittedAtMs!,
      rendered_at_ms: Date.now(),
      clock_skew_ms: _skewMs,
      server_fanout_at_ms: params.serverFanoutAtMs ?? null,
      client_received_at_ms: params.clientReceivedAtMs ?? null,
      pubsub_seq: params.pubsubSeq ?? null,
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
