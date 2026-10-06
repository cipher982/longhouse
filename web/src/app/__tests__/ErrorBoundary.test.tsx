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
    vi.restoreAllMocks();
  });

  it("reloads the document to retry a failed lazy page module", () => {
    const reload = vi.fn();
    render(
      <ErrorBoundary>
        <ThrowError error={new TypeError("Failed to fetch dynamically imported module: /assets/MachinesPage.js")} />
      </ErrorBoundary>,
    );
    vi.stubGlobal("window", { location: { reload } });

    expect(screen.getByText("Longhouse could not load this page. Retry the page load.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload to try again" }));

    expect(reload).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Try Again" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload Page" })).not.toBeInTheDocument();
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
