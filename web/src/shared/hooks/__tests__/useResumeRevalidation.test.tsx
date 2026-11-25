import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RESUME_REVALIDATION_GAP_MS, useResumeRevalidation } from "../useResumeRevalidation";

describe("useResumeRevalidation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-25T14:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("refetches and reports revalidating after returning from a hidden gap", async () => {
    let resolveFetch!: () => void;
    const refetch = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveFetch = resolve;
        }),
    );

    const { result, rerender } = renderHook(
      ({ visible }) => useResumeRevalidation(visible, refetch),
      { initialProps: { visible: true } },
    );

    expect(result.current).toBe(false);
    expect(refetch).not.toHaveBeenCalled();

    rerender({ visible: false });
    vi.advanceTimersByTime(RESUME_REVALIDATION_GAP_MS + 1_000);
    rerender({ visible: true });

    expect(refetch).toHaveBeenCalledTimes(1);
    expect(result.current).toBe(true);

    await act(async () => {
      resolveFetch();
      await Promise.resolve();
    });

    expect(result.current).toBe(false);
  });

  it("ignores brief tab switches shorter than the gap threshold", () => {
    const refetch = vi.fn(() => Promise.resolve());
    const { result, rerender } = renderHook(
      ({ visible }) => useResumeRevalidation(visible, refetch),
      { initialProps: { visible: true } },
    );

    rerender({ visible: false });
    vi.advanceTimersByTime(RESUME_REVALIDATION_GAP_MS - 500);
    rerender({ visible: true });

    expect(refetch).not.toHaveBeenCalled();
    expect(result.current).toBe(false);
  });
});
