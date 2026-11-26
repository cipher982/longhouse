import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AMBIENT_BLUR_GRACE_MS,
  POINTER_WAKE_LINGER_MS,
  getWindowActivity,
  resetWindowActivityForTests,
  useWindowActivity,
} from "../useWindowActivity";

describe("useWindowActivity", () => {
  let hidden = false;
  let focused = true;

  beforeEach(() => {
    vi.useFakeTimers();
    hidden = false;
    focused = true;
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => hidden,
    });
    vi.spyOn(document, "hasFocus").mockImplementation(() => focused);
    resetWindowActivityForTests();
  });

  afterEach(() => {
    resetWindowActivityForTests();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("transitions focused -> ambient after blur grace and wakes on focus or pointer", () => {
    const { result } = renderHook(() => useWindowActivity());
    expect(result.current).toBe("focused");
    expect(document.documentElement.dataset.windowActivity).toBe("focused");

    act(() => {
      focused = false;
      window.dispatchEvent(new Event("blur"));
      vi.advanceTimersByTime(AMBIENT_BLUR_GRACE_MS - 1);
    });
    expect(result.current).toBe("focused");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe("ambient");
    expect(document.documentElement.dataset.windowActivity).toBe("ambient");

    // Second-monitor pointer wake without clicking to focus.
    act(() => {
      window.dispatchEvent(new Event("pointerenter"));
    });
    expect(result.current).toBe("focused");
    expect(document.documentElement.dataset.windowActivity).toBe("focused");

    act(() => {
      vi.advanceTimersByTime(POINTER_WAKE_LINGER_MS);
    });
    expect(result.current).toBe("ambient");

    // Window focus restores focused immediately and cancels timers.
    act(() => {
      focused = true;
      window.dispatchEvent(new Event("focus"));
    });
    expect(result.current).toBe("focused");
    expect(getWindowActivity()).toBe("focused");
  });

  it("drops immediately to hidden on visibilitychange", () => {
    const { result } = renderHook(() => useWindowActivity());
    expect(result.current).toBe("focused");

    act(() => {
      hidden = true;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toBe("hidden");
    expect(document.documentElement.dataset.windowActivity).toBe("hidden");

    act(() => {
      hidden = false;
      focused = true;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toBe("focused");
  });
});
