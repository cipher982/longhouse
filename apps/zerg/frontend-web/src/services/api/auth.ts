import { request } from "./base";
import type { GmailConnectResponse } from "./types";

export async function connectGmailInbox(authCode: string): Promise<GmailConnectResponse> {
  return request<GmailConnectResponse>("/auth/google/gmail", {
    method: "POST",
    body: JSON.stringify({ auth_code: authCode }),
  });
}
