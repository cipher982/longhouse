import { describe, expect, test } from "bun:test";
import type { APIRequestContext } from "@playwright/test";
import { postRetryingUnavailable, retryDelayMs } from "./retryable-post";

// A fake request context that answers from a script, so the retry is proven
// without a loaded server.
function scripted(answers: Array<{ status: number; code?: string; retryAfter?: string }>) {
  let calls = 0;
  const request = {
    post: async () => {
      const a = answers[Math.min(calls++, answers.length - 1)];
      return {
        status: () => a.status,
        headers: () => (a.retryAfter ? { "retry-after": a.retryAfter } : {}),
        json: async () => ({ detail: { code: a.code } }),
        text: async () => "",
        ok: () => a.status < 300,
      };
    },
  } as unknown as APIRequestContext;
  return { request, calls: () => calls };
}

describe("postRetryingUnavailable", () => {
  test("retries storage_lane_busy until the lane drains (the 2026-11-16 CI failures)", async () => {
    const { request, calls } = scripted([
      { status: 503, code: "storage_lane_busy", retryAfter: "0" },
      { status: 503, code: "storage_lane_busy", retryAfter: "0" },
      { status: 200 },
    ]);
    const response = await postRetryingUnavailable(request, "/x", {}, 6);
    expect(response.status()).toBe(200);
    expect(calls()).toBe(3);
  });

  test("does not retry a refusal that is not transient", async () => {
    const { request, calls } = scripted([{ status: 503, code: "invalid_envelope" }]);
    const response = await postRetryingUnavailable(request, "/x", {}, 6);
    expect(response.status()).toBe(503);
    expect(calls()).toBe(1);
  });

  test("honors Retry-After, capped, else backs off exponentially", () => {
    expect(retryDelayMs("2", 1)).toBe(2000);
    expect(retryDelayMs("60", 1)).toBe(5000);
    expect(retryDelayMs(undefined, 3)).toBe(1000);
  });
});
