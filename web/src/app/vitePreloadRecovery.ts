declare global {
  interface Window {
    __longhouseVitePreloadFailure?: boolean;
  }
}

const RETRY_AT_KEY = "longhouse:vite-preload-retry-at";
const RETRY_COOLDOWN_MS = 30_000;

export function installVitePreloadRecovery(targetWindow: Window = window): void {
  targetWindow.addEventListener("vite:preloadError", (event) => {
    targetWindow.__longhouseVitePreloadFailure = true;
    let lastRetryAt: number;
    try {
      lastRetryAt = Number(targetWindow.sessionStorage.getItem(RETRY_AT_KEY) ?? 0);
    } catch {
      return;
    }

    // Avoid a reload loop if the same missing asset remains unavailable.
    if (Number.isFinite(lastRetryAt) && Date.now() - lastRetryAt < RETRY_COOLDOWN_MS) return;

    try {
      targetWindow.sessionStorage.setItem(RETRY_AT_KEY, String(Date.now()));
    } catch {
      return;
    }
    event.preventDefault();
    targetWindow.location.reload();
  });
}
