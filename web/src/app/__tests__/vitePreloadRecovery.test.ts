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
    const eventTarget = new EventTarget();
    const targetWindow = {
      location: { pathname: "/machines", reload },
      sessionStorage: window.sessionStorage,
      addEventListener: eventTarget.addEventListener.bind(eventTarget),
      dispatchEvent: eventTarget.dispatchEvent.bind(eventTarget),
    } as unknown as Window;
    targetWindow.sessionStorage.removeItem(RETRY_AT_KEY);
    installVitePreloadRecovery(targetWindow);

    const first = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(first);
    expect(first.defaultPrevented).toBe(true);
    expect(targetWindow.__longhouseVitePreloadFailure).toBe(true);
    expect(reload).toHaveBeenCalledOnce();
    expect(Number(targetWindow.sessionStorage.getItem(RETRY_AT_KEY))).toBeGreaterThan(0);

    const repeated = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(repeated);
    expect(repeated.defaultPrevented).toBe(false);
    expect(reload).toHaveBeenCalledOnce();

    targetWindow.sessionStorage.setItem(RETRY_AT_KEY, String(Date.now() - 30_001));
    const afterCooldown = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(afterCooldown);
    expect(afterCooldown.defaultPrevented).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("does not auto-reload routes outside Machines", () => {
    const reload = vi.fn();
    const eventTarget = new EventTarget();
    const targetWindow = {
      location: { pathname: "/timeline", reload },
      sessionStorage: window.sessionStorage,
      addEventListener: eventTarget.addEventListener.bind(eventTarget),
      dispatchEvent: eventTarget.dispatchEvent.bind(eventTarget),
    } as unknown as Window;
    targetWindow.sessionStorage.removeItem(RETRY_AT_KEY);
    installVitePreloadRecovery(targetWindow);

    const event = new Event("vite:preloadError", { cancelable: true });
    targetWindow.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    expect(targetWindow.__longhouseVitePreloadFailure).toBe(true);
  });
});
