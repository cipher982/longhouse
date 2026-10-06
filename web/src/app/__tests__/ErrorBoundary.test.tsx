import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../ErrorBoundary";
import { installVitePreloadRecovery } from "@/shared/lib/vitePreloadRecovery";

function ThrowError({ error }: { error: Error }): never {
  throw error;
}

describe("ErrorBoundary", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("uses the Vite event's original error for the reload fallback", () => {
    const reload = vi.fn();
    const error = new TypeError("opaque module-load failure");
    const eventTarget = new EventTarget();
    const targetWindow = {
      location: { pathname: "/timeline", reload },
      sessionStorage: window.sessionStorage,
      addEventListener: eventTarget.addEventListener.bind(eventTarget),
      dispatchEvent: eventTarget.dispatchEvent.bind(eventTarget),
    } as unknown as Window;
    installVitePreloadRecovery(targetWindow);
    targetWindow.dispatchEvent(Object.assign(new Event("vite:preloadError", { cancelable: true }), { payload: error }));

    const originalWindow = window;
    vi.stubGlobal(
      "window",
      new Proxy(originalWindow, {
        get(target, property) {
          if (property === "location") return { reload };
          const value = Reflect.get(target, property, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      }),
    );
    render(
      <ErrorBoundary>
        <ThrowError error={error} />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Longhouse could not load this page. Retry the page load.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload to try again" }));
    expect(reload).toHaveBeenCalledOnce();
  });

  it("passes module-load state to a custom fallback", () => {
    const error = new Error("custom fallback load failure");
    const eventTarget = new EventTarget();
    const targetWindow = {
      location: { pathname: "/timeline", reload: vi.fn() },
      sessionStorage: window.sessionStorage,
      addEventListener: eventTarget.addEventListener.bind(eventTarget),
      dispatchEvent: eventTarget.dispatchEvent.bind(eventTarget),
    } as unknown as Window;
    installVitePreloadRecovery(targetWindow);
    targetWindow.dispatchEvent(Object.assign(new Event("vite:preloadError"), { payload: error }));

    render(
      <ErrorBoundary
        fallback={({ moduleLoadFailure }) => <p>{moduleLoadFailure ? "Module failure" : "Other failure"}</p>}
      >
        <ThrowError error={error} />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Module failure")).toBeInTheDocument();
  });

  it("uses generic recovery copy for errors without messages", () => {
    render(
      <ErrorBoundary>
        <ThrowError error={new Error("")} />
      </ErrorBoundary>,
    );

    expect(screen.getByText(/An unexpected error occurred/)).toBeInTheDocument();
  });

  it("keeps the in-place retry for render errors", () => {
    render(
      <ErrorBoundary>
        <ThrowError error={new Error("render failed")} />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("button", { name: "Try Again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload Page" })).toBeInTheDocument();
  });
});
