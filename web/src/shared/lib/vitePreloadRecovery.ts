const installedWindows = new WeakSet<Window>();
const preloadFailures = new WeakSet<object>();

export function isVitePreloadFailure(error?: Error): boolean {
  return error !== undefined && preloadFailures.has(error);
}

export function installVitePreloadRecovery(targetWindow: Window = window): void {
  if (installedWindows.has(targetWindow)) return;
  installedWindows.add(targetWindow);

  targetWindow.addEventListener("vite:preloadError", (event) => {
    const payload = (event as Event & { payload?: unknown }).payload;
    if (payload !== null && typeof payload === "object") preloadFailures.add(payload);
  });
}
