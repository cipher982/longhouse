import { describe, it, expect, vi } from 'vitest';
const { fetchWithRefreshMock } = vi.hoisted(() => ({
  fetchWithRefreshMock: vi.fn(),
}));


// Mock the config module before importing buildUrl
vi.mock("@/features/auth/auth-refresh", () => ({
  fetchWithRefresh: fetchWithRefreshMock,
}));

vi.mock('@/shared/lib/config', () => ({
  config: { apiBaseUrl: '/api' },
}));

vi.mock('@/shared/lib/logger', () => ({
  logger: {
    error: vi.fn(),
  },
}));

import { hostLinkStore } from "@/shared/hostLink/store";
import { ApiError, buildUrl, request } from "../base";

describe('buildUrl', () => {
  it('prepends /api to a path without prefix', () => {
    expect(buildUrl('/system/capabilities')).toBe('/api/system/capabilities');
  });

  it('prepends /api to path without leading slash', () => {
    expect(buildUrl('system/capabilities')).toBe('/api/system/capabilities');
  });

  it('strips duplicate /api prefix to prevent double-prefix bug', () => {
    // Passing "/api/foo" should produce "/api/foo", not "/api/api/foo"
    expect(buildUrl('/api/system/capabilities')).toBe('/api/system/capabilities');
    expect(buildUrl('/api/health')).toBe('/api/health');
  });

  it('handles nested paths correctly', () => {
    expect(buildUrl('/sessions/abc/events')).toBe('/api/sessions/abc/events');
  });
});

describe('ApiError', () => {
  it('formats FastAPI array-shaped validation detail instead of a bare 422', () => {
    const error = new ApiError({
      url: '/api/sessions/sess-1/input',
      status: 422,
      body: {
        detail: [
          {
            type: 'string_too_short',
            loc: ['body', 'text'],
            msg: 'String should have at least 1 character',
          },
        ],
      },
    });

    expect(error.message).toBe('text: String should have at least 1 character');
  });
});

describe("host-link API observations", () => {
  it("records K1 restart refusals and accepts any successful write as serving evidence", async () => {
    fetchWithRefreshMock.mockReset();
    hostLinkStore.observeLifecycle({
      state: "serving",
      runtime_epoch: "runtime-before",
    });
    const now = Date.now();
    const body = {
      code: "runtime_restarting",
      retryable: true,
      runtime_epoch: "runtime-candidate",
      admission: "draining",
      claim: {
        type: "host.lifecycle",
        state: "updating",
        runtime_epoch: "runtime-candidate",
        attempt_id: "attempt-1",
        phase: "drain",
        expected_back_by: new Date(now + 30_000).toISOString(),
        deadline: new Date(now + 60_000).toISOString(),
        cutoff: new Date(now + 90_000).toISOString(),
      },
    };
    fetchWithRefreshMock.mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(
      request("/sessions/session-1/input", { method: "POST" }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(hostLinkStore.getSnapshot()).toMatchObject({
      state: "updating",
      runtimeEpoch: "runtime-candidate",
      claim: { attempt_id: "attempt-1" },
    });

    fetchWithRefreshMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await request("/sessions/session-1/input", { method: "POST" });
    expect(hostLinkStore.getSnapshot()).toMatchObject({
      state: "serving",
      claim: null,
    });
  });
});
