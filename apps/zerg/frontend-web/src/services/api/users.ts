import { request } from "./base";
import type { UserContext, UserContextResponse } from "./types";

export async function getUserContext(): Promise<UserContextResponse> {
  return request<UserContextResponse>(`/users/me/context`);
}

export async function updateUserContext(context: UserContext): Promise<UserContextResponse> {
  return request<UserContextResponse>(`/users/me/context`, {
    method: "PATCH",
    body: JSON.stringify({ context }),
  });
}

export async function replaceUserContext(context: UserContext): Promise<UserContextResponse> {
  return request<UserContextResponse>(`/users/me/context`, {
    method: "PUT",
    body: JSON.stringify({ context }),
  });
}
