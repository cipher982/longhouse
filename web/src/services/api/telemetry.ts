import { request } from "./base";

export interface ClientRenderBeaconItem {
  session_id: string | null;
  event_id: string | null;
  surface: string | null;
  managed: boolean | null;
  latency_ms: number | null;
  emitted_at_ms: number | null;
  rendered_at_ms: number | null;
  clock_skew_ms: number | null;
  server_fanout_at_ms: number | null;
  client_received_at_ms: number | null;
  pubsub_seq: number | null;
  webkit?: ClientRenderWebKitDiagnostics | null;
  observed_at: string | null;
  received_at: string | null;
}

export interface ClientRenderWebKitDiagnostics {
  stage?: string | null;
  payload_byte_size?: number | null;
  row_count?: number | null;
  latest_item_id?: string | null;
  render_sequence?: number | null;
  js_failure_count?: number | null;
  should_stick_to_bottom?: boolean | null;
  web_view_loaded?: boolean | null;
  error_description?: string | null;
}

export interface RecentClientRenderBeaconsResponse {
  items: ClientRenderBeaconItem[];
}

interface FetchRecentClientRenderBeaconsOptions {
  sessionId?: string | null;
  eventId?: string | number | null;
  limit?: number;
}

export function fetchRecentClientRenderBeacons(
  options: FetchRecentClientRenderBeaconsOptions = {},
): Promise<RecentClientRenderBeaconsResponse> {
  const params = new URLSearchParams();
  if (options.sessionId) {
    params.set("session_id", options.sessionId);
  }
  if (options.eventId !== null && options.eventId !== undefined) {
    params.set("event_id", String(options.eventId));
  }
  params.set("limit", String(options.limit ?? 8));

  return request<RecentClientRenderBeaconsResponse>(
    `/telemetry/client-render/recent?${params.toString()}`,
    { method: "GET" },
  );
}
