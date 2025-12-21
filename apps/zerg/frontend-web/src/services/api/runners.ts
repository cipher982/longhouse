import { request } from "./base";
import type {
  Runner,
  EnrollTokenResponse,
  RunnerUpdate,
  RunnerListResponse,
  RotateSecretResponse,
} from "./types";

export async function createEnrollToken(): Promise<EnrollTokenResponse> {
  return request<EnrollTokenResponse>(`/runners/enroll-token`, {
    method: "POST",
  });
}

export async function fetchRunners(): Promise<Runner[]> {
  const response = await request<RunnerListResponse>(`/runners/`);
  return response.runners;
}

export async function fetchRunner(runnerId: number): Promise<Runner> {
  return request<Runner>(`/runners/${runnerId}`);
}

export async function updateRunner(runnerId: number, payload: RunnerUpdate): Promise<Runner> {
  return request<Runner>(`/runners/${runnerId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function revokeRunner(runnerId: number): Promise<{ success: boolean }> {
  return request<{ success: boolean }>(`/runners/${runnerId}/revoke`, {
    method: "POST",
  });
}

export async function rotateRunnerSecret(runnerId: number): Promise<RotateSecretResponse> {
  return request<RotateSecretResponse>(`/runners/${runnerId}/rotate-secret`, {
    method: "POST",
  });
}
