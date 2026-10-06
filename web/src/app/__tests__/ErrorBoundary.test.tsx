import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../ErrorBoundary";

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
    delete window.__longhouseVitePreloadFailure;
    vi.restoreAllMocks();
  });

  it("reloads for Vite-signaled module failures without parsing browser error text", () => {
    const reload = vi.fn();
    const originalWindow = window;
    vi.stubGlobal(
      "window",
      new Proxy(originalWindow, {
        get(target, property) {
          if (property === "__longhouseVitePreloadFailure") return true;
          if (property === "location") return { reload };
          const value = Reflect.get(target, property, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      }),
    );

    render(
      <ErrorBoundary>
        <ThrowError error={new TypeError("opaque module load failure")} />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Longhouse could not load this page. Retry the page load.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload to try again" }));

    expect(reload).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Try Again" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload Page" })).not.toBeInTheDocument();
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
