import type { APIRequestContext, APIResponse } from "@playwright/test";

// 503 codes the server marks as transient ("retry the same envelope"). A real
// client retries these; test setup that fails on the first one reports load on
// the CI runner as a product failure.
const RETRYABLE_UNAVAILABLE = new Set(["catalog_unavailable", "storage_worker_unavailable"]);

export async function postRetryingUnavailable(
  request: APIRequestContext,
  url: string,
  options: Parameters<APIRequestContext["post"]>[1],
  attempts = 6,
): Promise<APIResponse> {
  for (let attempt = 1; ; attempt += 1) {
    const response = await request.post(url, options);
    if (response.status() !== 503 || attempt === attempts) return response;
    const code = (await response.json().catch(() => null))?.detail?.code;
    if (!RETRYABLE_UNAVAILABLE.has(code)) return response;
    await new Promise(resolve => setTimeout(resolve, 250 * 2 ** (attempt - 1)));
  }
}
