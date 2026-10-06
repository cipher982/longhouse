import { afterEach, describe, expect, it, vi } from "vitest";
import { installVitePreloadRecovery, isVitePreloadFailure } from "@/shared/lib/vitePreloadRecovery";

describe("installVitePreloadRecovery", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["/machines", "/machines/cinder", "/timeline"])(
    "records failed imports on %s without reloading",
    (pathname) => {
      const reload = vi.fn();
      const eventTarget = new EventTarget();
      const addEventListener = vi.fn(eventTarget.addEventListener.bind(eventTarget));
      const targetWindow = {
        location: { pathname, reload },
        addEventListener,
        dispatchEvent: eventTarget.dispatchEvent.bind(eventTarget),
      } as unknown as Window;
      installVitePreloadRecovery(targetWindow);
      installVitePreloadRecovery(targetWindow);
      expect(addEventListener).toHaveBeenCalledOnce();

      const failure = new Error("module import failed");
      const event = Object.assign(new Event("vite:preloadError", { cancelable: true }), { payload: failure });
      targetWindow.dispatchEvent(event);

      expect(event.defaultPrevented).toBe(false);
      expect(reload).not.toHaveBeenCalled();
      expect(isVitePreloadFailure(failure)).toBe(true);
      expect(isVitePreloadFailure(new Error("module import failed"))).toBe(false);
    },
  );
});
