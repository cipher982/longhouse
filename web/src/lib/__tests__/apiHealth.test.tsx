import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { useApiHealth } from "../apiHealth";

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
      expect(result.current).toBe(trackedError);
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
});
