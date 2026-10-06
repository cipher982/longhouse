import { afterEach, describe, expect, it, vi } from "vitest";
import { installVitePreloadRecovery } from "../vitePreloadRecovery";

const RETRY_AT_KEY = "longhouse:vite-preload-retry-at";

describe("installVitePreloadRecovery", () => {
  afterEach(() => {
    window.sessionStorage.removeItem(RETRY_AT_KEY);
    delete window.__longhouseVitePreloadFailure;
    vi.restoreAllMocks();
  });

  it("reloads on a preload failure and throttles repeated retries", () => {
    const reload = vi.fn();
    const currentTime = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    const targetWindow = new Proxy(window, {
      get(target, property) {
        if (property === "location") return { reload };
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    }) as Window;
    targetWindow.sessionStorage.removeItem(RETRY_AT_KEY);
    installVitePreloadRecovery(targetWindow);

    const first = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(first);
    expect(first.defaultPrevented).toBe(true);
    expect(targetWindow.__longhouseVitePreloadFailure).toBe(true);
    expect(reload).toHaveBeenCalledOnce();
    expect(targetWindow.sessionStorage.getItem(RETRY_AT_KEY)).toBe("1000000");

    const repeated = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(repeated);
    expect(repeated.defaultPrevented).toBe(false);
    expect(reload).toHaveBeenCalledOnce();

    currentTime.mockReturnValue(1_030_000);
    const afterCooldown = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(afterCooldown);
    expect(afterCooldown.defaultPrevented).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });
});
