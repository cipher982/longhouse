import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceShell } from "../WorkspaceShell";

const WORKSPACE_LAYOUT_STORAGE_KEY = "zerg:session-workspace-layout:v1";

class MockResizeObserver {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords() {
    return [];
  }
}

function renderShell() {
  return render(
    <WorkspaceShell
      header={<div>Header</div>}
      sidebar={<div>Sidebar</div>}
      main={<div>Main</div>}
      inspector={<div>Inspector</div>}
    />,
  );
}

describe("WorkspaceShell", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("upgrades the untouched legacy default layout to the wider sidebar", async () => {
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({ sidebarWidth: 248, inspectorWidth: 336 }),
    );

    const { container } = renderShell();
    const shell = container.firstElementChild as HTMLDivElement;

    expect(shell.style.getPropertyValue("--workspace-sidebar-width")).toBe("288px");
    expect(shell.style.getPropertyValue("--workspace-inspector-width")).toBe("336px");

    await waitFor(() => {
      expect(
        JSON.parse(window.localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY) || "{}"),
      ).toMatchObject({
        sidebarWidth: 288,
        inspectorWidth: 336,
      });
    });
  });

  it("preserves customized stored pane widths", async () => {
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({ sidebarWidth: 320, inspectorWidth: 380 }),
    );

    const { container } = renderShell();
    const shell = container.firstElementChild as HTMLDivElement;

    expect(shell.style.getPropertyValue("--workspace-sidebar-width")).toBe("320px");
    expect(shell.style.getPropertyValue("--workspace-inspector-width")).toBe("380px");

    await waitFor(() => {
      expect(
        JSON.parse(window.localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY) || "{}"),
      ).toMatchObject({
        sidebarWidth: 320,
        inspectorWidth: 380,
      });
    });
  });
});
