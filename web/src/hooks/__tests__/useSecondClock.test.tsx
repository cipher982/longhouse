import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSecondClock } from "../useSecondClock";

const visibilityMocks = vi.hoisted(() => ({
  useDocumentVisible: vi.fn(),
}));

vi.mock("../useDocumentVisible", () => visibilityMocks);

describe("useSecondClock", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibilityMocks.useDocumentVisible.mockReturnValue(true);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("aligns to the next second and then ticks once per second", () => {
    vi.setSystemTime(new Date("2026-03-22T22:04:30.250Z"));

    const { result } = renderHook(() => useSecondClock(true));

    expect(result.current).toBe(Date.parse("2026-03-22T22:04:30.250Z"));

    act(() => {
      vi.advanceTimersByTime(749);
    });
    expect(result.current).toBe(Date.parse("2026-03-22T22:04:30.250Z"));

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe(Date.parse("2026-03-22T22:04:31.000Z"));

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(Date.parse("2026-03-22T22:04:32.000Z"));
  });

  it("does not tick while hidden and resumes from current time when visible again", () => {
    vi.setSystemTime(new Date("2026-03-22T22:04:30.250Z"));
    visibilityMocks.useDocumentVisible.mockReturnValue(false);

    const { result, rerender } = renderHook(() => useSecondClock(true));

    expect(result.current).toBe(Date.parse("2026-03-22T22:04:30.250Z"));

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(Date.parse("2026-03-22T22:04:30.250Z"));

    visibilityMocks.useDocumentVisible.mockReturnValue(true);
    rerender();

    expect(result.current).toBe(Date.parse("2026-03-22T22:04:35.250Z"));

    act(() => {
      vi.advanceTimersByTime(750);
    });
    expect(result.current).toBe(Date.parse("2026-03-22T22:04:36.000Z"));
  });
});
