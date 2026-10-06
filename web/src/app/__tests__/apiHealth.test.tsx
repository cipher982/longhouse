import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { classifyApiHealthError, useApiHealth } from "../apiHealth";
import { ApiError } from "@/shared/api/base";

function buildWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useApiHealth", () => {
  it("surfaces tracked query errors and clears on recovery", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const wrapper = buildWrapper(queryClient);
    const queryKey = ["agent-sessions", { provider: "claude" }] as const;
    const trackedError = new Error("backend unavailable");

    const { result } = renderHook(() => useApiHealth(), { wrapper });
    expect(result.current).toBeNull();

    await act(async () => {
      await queryClient
        .fetchQuery({
          queryKey,
          queryFn: async () => {
            throw trackedError;
          },
          meta: { apiHealth: true },
          retry: false,
        })
        .catch(() => undefined);
    });

    await waitFor(() => {
      expect(result.current?.error).toBe(trackedError);
      expect(result.current?.label).toBe("Can't reach Longhouse");
    });

    act(() => {
      queryClient.setQueryData(queryKey, {
        sessions: [],
        total: 0,
        has_real_sessions: true,
      });
    });

    await waitFor(() => {
      expect(result.current).toBeNull();
    });
  });

  it("ignores untracked query errors", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    const wrapper = buildWrapper(queryClient);
    const queryKey = ["runnerStatus"] as const;

    const { result } = renderHook(() => useApiHealth(), { wrapper });

    await act(async () => {
      await queryClient
        .fetchQuery({
          queryKey,
          queryFn: async () => {
            throw new Error("transient runner failure");
          },
          retry: false,
        })
        .catch(() => undefined);
    });

    await waitFor(() => {
      expect(result.current).toBeNull();
    });
  });

  it("ignores a 4xx on a tracked query so one route never flips the pill", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = buildWrapper(queryClient);
    const { result } = renderHook(() => useApiHealth(), { wrapper });

    await act(async () => {
      await queryClient
        .fetchQuery({
          queryKey: ["agent-sessions", "missing"],
          queryFn: async () => {
            throw new ApiError({ url: "/api/x", status: 404, body: { detail: "Not found" } });
          },
          meta: { apiHealth: true },
          retry: false,
        })
        .catch(() => undefined);
    });

    await waitFor(() => {
      expect(result.current).toBeNull();
    });
  });
});

describe("classifyApiHealthError", () => {
  it("names a JSON 5xx as a server problem with status and code in the detail", () => {
    const problem = classifyApiHealthError(
      new ApiError({
        url: "/api/timeline/sessions",
        status: 503,
        body: { detail: { code: "shadow_fact_head_limit_exceeded", message: "Too many facts." } },
      }),
    );
    expect(problem?.kind).toBe("server_error");
    expect(problem?.label).toBe("Longhouse had a problem");
    expect(problem?.detail).toBe("HTTP 503 · shadow_fact_head_limit_exceeded · Too many facts.");
  });

  it("treats a 5xx without a JSON body as the server not answering", () => {
    const problem = classifyApiHealthError(
      new ApiError({ url: "/api/x", status: 502, body: "<html>Bad gateway</html>" }),
    );
    expect(problem?.kind).toBe("unreachable");
    expect(problem?.label).toBe("Can't reach Longhouse");
  });

  it("treats a network failure as unreachable", () => {
    const problem = classifyApiHealthError(new TypeError("Failed to fetch"));
    expect(problem?.kind).toBe("unreachable");
  });

  it("ignores client errors, including 401", () => {
    expect(classifyApiHealthError(new ApiError({ url: "/api/x", status: 401, body: { detail: "No" } }))).toBeNull();
    expect(classifyApiHealthError(new ApiError({ url: "/api/x", status: 409, body: { detail: "No" } }))).toBeNull();
  });
});
