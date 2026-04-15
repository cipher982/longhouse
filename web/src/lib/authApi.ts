/**
 * Low-level auth API calls used by LoginPage and any future auth surfaces.
 * These are plain fetch wrappers — no React, no hooks.
 */

import config from "./config";

export async function loginWithPassword(
  password: string,
): Promise<{ ok: boolean; error?: string }> {
  const response = await fetch(`${config.apiBaseUrl}/auth/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ password }),
  });
  if (response.ok) return { ok: true };
  if (response.status === 400) return { ok: false, error: "Password auth not configured" };
  if (response.status === 429) {
    const retryAfter = response.headers.get("Retry-After");
    const suffix = retryAfter ? ` Try again in ${retryAfter}s.` : " Try again later.";
    return { ok: false, error: `Too many attempts.${suffix}` };
  }
  return { ok: false, error: "Invalid password" };
}

export async function loginWithDevAccount(): Promise<void> {
  const response = await fetch(`${config.apiBaseUrl}/auth/dev-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "Dev login failed");
  }
}
