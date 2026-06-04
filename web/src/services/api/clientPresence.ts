import { request } from "./base";

export interface WebClientPresenceHeartbeat {
  client_id: string;
  client_type: "web";
  visible: boolean;
  route: string | null;
  session_id: string | null;
}

export interface WebClientPresenceResponse extends WebClientPresenceHeartbeat {
  last_seen_at: string;
}

export async function postWebClientPresence(
  heartbeat: WebClientPresenceHeartbeat,
): Promise<WebClientPresenceResponse> {
  return request<WebClientPresenceResponse>("/users/me/client-presence", {
    method: "POST",
    body: JSON.stringify(heartbeat),
  });
}
