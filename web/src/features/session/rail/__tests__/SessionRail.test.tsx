import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentSession, TimelineSessionsListResponse } from "@/shared/api/agents";
import { makeSessionStateFacts } from "@/shared/test/sessionState";
import { SessionRailFrame, isSwitcherHotkey, railHotkeyIndex, railHotkeyLabel } from "../SessionRail";
import { filterSwitcherRows, previewFromWorkspace, trimPreviewMarkdown } from "../SessionSwitcher";
import type { AgentSessionWorkspaceResponse } from "@/shared/api/agents";
import { RAIL_PREFETCH_COUNT, railPrefetchAllowed } from "../useRailPrefetch";

const fetchAgentSessionsMock = vi.hoisted(() => vi.fn());
const fetchWorkspaceMock = vi.hoisted(() => vi.fn());
const navigateMock = vi.hoisted(() => vi.fn());

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("@/shared/api/agents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/agents")>();
  return {
    ...actual,
    fetchAgentSessions: fetchAgentSessionsMock,
    fetchAgentSessionWorkspace: fetchWorkspaceMock,
  };
});

function session(id: string, title: string): AgentSession {
  return {
    id,
    provider: "claude",
    project: "zerg",
    device_id: "cinder",
    summary_title: title,
    timeline_title: title,
    user_messages: 1,
    assistant_messages: 1,
    tool_calls: 0,
    session_state: makeSessionStateFacts({ access: "live_control", activity: "quiescent" }),
  } as unknown as AgentSession;
}

function list(ids: string[]): TimelineSessionsListResponse {
  return {
    sessions: ids.map((id) => ({
      thread_id: id,
      timeline_anchor_at: null,
      head: session(id, `Session ${id}`),
      continuation_count: 0,
      started_origin_label: null,
      head_origin_label: null,
    })),
    total: ids.length,
    has_real_sessions: true,
  };
}

function Page() {
  return <output data-testid="page" />;
}

function renderRail(activeId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/timeline/${activeId}`]}>
        <Routes>
          <Route
            path="/timeline/:sessionId"
            element={
              <SessionRailFrame activeSessionId={activeId} returnTo="/timeline">
                <Page />
              </SessionRailFrame>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("rail hotkeys", () => {
  const press = (code: string, mods: Partial<KeyboardEvent> = {}) => ({
    code,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    shiftKey: false,
    ...mods,
  });

  it("uses Control on a Mac, since the browser keeps Command-number", () => {
    expect(railHotkeyIndex(press("Digit2", { ctrlKey: true }), true)).toBe(1);
    expect(railHotkeyIndex(press("Digit2", { metaKey: true }), true)).toBeNull();
    expect(railHotkeyIndex(press("Digit2", { ctrlKey: true, shiftKey: true }), true)).toBeNull();
    expect(railHotkeyLabel(1, true)).toBe("⌃2");
  });

  it("uses Alt elsewhere and ignores non-digit keys", () => {
    expect(railHotkeyIndex(press("Digit9", { altKey: true }), false)).toBe(8);
    expect(railHotkeyIndex(press("Digit9", { ctrlKey: true }), false)).toBeNull();
    expect(railHotkeyIndex(press("Digit0", { altKey: true }), false)).toBeNull();
    expect(railHotkeyIndex(press("KeyA", { altKey: true }), false)).toBeNull();
    expect(railHotkeyLabel(0, false)).toBe("Alt+1");
  });
});

describe("rail prefetch gate", () => {
  it("stands down on Data Saver and 2G-class links", () => {
    expect(railPrefetchAllowed({ connection: { saveData: true } } as unknown as Navigator)).toBe(false);
    expect(railPrefetchAllowed({ connection: { effectiveType: "2g" } } as unknown as Navigator)).toBe(false);
    expect(railPrefetchAllowed({ connection: { effectiveType: "4g" } } as unknown as Navigator)).toBe(true);
    expect(railPrefetchAllowed({} as Navigator)).toBe(true);
  });
});

describe("SessionRailFrame", () => {
  const platform = Object.getOwnPropertyDescriptor(window.navigator, "platform");

  beforeEach(() => {
    Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
    fetchAgentSessionsMock.mockResolvedValue(list(["a", "b", "c"]));
    fetchWorkspaceMock.mockResolvedValue({});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    if (platform) Object.defineProperty(window.navigator, "platform", platform);
    else delete (window.navigator as { platform?: string }).platform;
  });

  it("lists recent sessions, marks the open one, and switches on Control-number", async () => {
    renderRail("a");
    const rows = await screen.findAllByTestId("session-rail-row");
    expect(rows.map((row) => row.getAttribute("data-session-id"))).toEqual(["a", "b", "c"]);
    expect(rows[0]).toHaveAttribute("aria-current", "page");
    expect(rows[1]).toHaveTextContent("⌃2");

    fireEvent.keyDown(window, { code: "Digit3", ctrlKey: true });
    expect(navigateMock).toHaveBeenCalledWith("/timeline/c", { state: { from: "/timeline" } });

    // The open session's own key does nothing; past the list does nothing.
    navigateMock.mockClear();
    fireEvent.keyDown(window, { code: "Digit1", ctrlKey: true });
    fireEvent.keyDown(window, { code: "Digit7", ctrlKey: true });
    expect(navigateMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("page")).toBeInTheDocument();
  });

  it("warms the other listed sessions while idle, never the open one", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchAgentSessionsMock.mockResolvedValue(list(Array.from({ length: 12 }, (_, i) => `s${i}`)));
    renderRail("s0");
    await screen.findAllByTestId("session-rail-row");
    for (let i = 0; i < RAIL_PREFETCH_COUNT + 2; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(700);
      });
    }
    const warmed = fetchWorkspaceMock.mock.calls.map(([id]) => id);
    expect(warmed).not.toContain("s0");
    expect(warmed).toEqual(["s1", "s2", "s3", "s4", "s5", "s6", "s7"]);
    expect(fetchWorkspaceMock.mock.calls[0][1]).toMatchObject({ limit: 200, branch_mode: "head" });
  });

  it("ignores key repeat, and Alt-digit typed into a field off a Mac", async () => {
    renderRail("a");
    await screen.findAllByTestId("session-rail-row");
    fireEvent.keyDown(window, { code: "Digit2", ctrlKey: true, repeat: true });
    expect(navigateMock).not.toHaveBeenCalled();

    Object.defineProperty(window.navigator, "platform", { value: "Linux x86_64", configurable: true });
    const view = renderRail("a");
    await screen.findAllByTestId("session-rail-row");
    const field = document.createElement("textarea");
    view.container.appendChild(field);
    fireEvent.keyDown(field, { code: "Digit2", altKey: true });
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("does not warm anything while the tab is hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    try {
      renderRail("a");
      await screen.findAllByTestId("session-rail-row");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchWorkspaceMock).not.toHaveBeenCalled();
    } finally {
      delete (document as { hidden?: boolean }).hidden;
    }
  });

  it("does not warm anything on Data Saver", async () => {
    Object.defineProperty(window.navigator, "connection", {
      value: { saveData: true },
      configurable: true,
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderRail("a");
      await screen.findAllByTestId("session-rail-row");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchWorkspaceMock).not.toHaveBeenCalled();
    } finally {
      delete (window.navigator as { connection?: unknown }).connection;
    }
  });
});

describe("session switcher", () => {
  const platform = Object.getOwnPropertyDescriptor(window.navigator, "platform");

  beforeEach(() => {
    Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
    fetchAgentSessionsMock.mockResolvedValue(list(["a", "b", "c"]));
    fetchWorkspaceMock.mockResolvedValue({
      projection: {
        items: [
          { kind: "event", event: { role: "user", content_text: "Ship it?", tool_name: null } },
          { kind: "event", event: { role: "assistant", content_text: "Shipped in abc123.", tool_name: null } },
        ],
      },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
    if (platform) Object.defineProperty(window.navigator, "platform", platform);
    else delete (window.navigator as { platform?: string }).platform;
  });

  it("takes Command-K on a Mac and Control-K elsewhere", () => {
    const key = (mods: Partial<KeyboardEvent>) => ({ key: "k", ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...mods });
    expect(isSwitcherHotkey(key({ metaKey: true }), true)).toBe(true);
    expect(isSwitcherHotkey(key({ ctrlKey: true }), true)).toBe(false);
    expect(isSwitcherHotkey(key({ ctrlKey: true }), false)).toBe(true);
    expect(isSwitcherHotkey(key({ metaKey: true, shiftKey: true }), true)).toBe(false);
  });

  it("filters on every term across title, machine and provider", () => {
    const rows = [
      { title: "Fix the flaky reconnect test", host: "cinder", provider: "codex" },
      { title: "OMP hang", host: "cube", provider: "omp" },
    ];
    expect(filterSwitcherRows(rows, "cube hang")).toEqual([rows[1]]);
    expect(filterSwitcherRows(rows, "  ")).toEqual(rows);
    expect(filterSwitcherRows(rows, "codex cube")).toEqual([]);
  });

  it("previews the newest ask and reply, skipping tool rows", () => {
    const workspace = {
      projection: {
        items: [
          { kind: "event", event: { role: "user", content_text: "first ask", tool_name: null } },
          { kind: "event", event: { role: "assistant", content_text: "reply", tool_name: null } },
          { kind: "event", event: { role: "assistant", content_text: "tool chatter", tool_name: "Bash" } },
        ],
      },
    } as unknown as AgentSessionWorkspaceResponse;
    expect(previewFromWorkspace(workspace)).toEqual({ ask: "first ask", reply: "reply", askIsNewer: false });
    expect(previewFromWorkspace(undefined)).toEqual({ ask: null, reply: null, askIsNewer: false });

    const waiting = {
      projection: {
        items: [
          { kind: "event", event: { role: "assistant", content_text: "old reply", tool_name: null } },
          { kind: "event", event: { role: "user", content_text: "new ask", tool_name: null } },
        ],
      },
    } as unknown as AgentSessionWorkspaceResponse;
    expect(previewFromWorkspace(waiting)).toEqual({ ask: "new ask", reply: "old reply", askIsNewer: true });
  });

  it("opens on Command-K, filters, previews and opens the match on Enter", async () => {
    renderRail("a");
    await screen.findAllByTestId("session-rail-row");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByLabelText("Filter sessions");
    fireEvent.change(input, { target: { value: "session b" } });
    expect(screen.getAllByTestId("session-switcher-row")).toHaveLength(1);
    expect(await screen.findByText("Shipped in abc123.")).toBeInTheDocument();
    // Jump keys belong to the switcher's owner page only while it is closed.
    fireEvent.keyDown(window, { code: "Digit3", ctrlKey: true });
    expect(navigateMock).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigateMock).toHaveBeenCalledWith("/timeline/b", { state: { from: "/timeline" } });
    expect(screen.queryByTestId("session-switcher")).not.toBeInTheDocument();
  });
});

describe("session switcher preview fetching", () => {
  const platform = Object.getOwnPropertyDescriptor(window.navigator, "platform");

  beforeEach(() => {
    Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
    fetchAgentSessionsMock.mockResolvedValue(list(["a", "b", "c", "d"]));
    fetchWorkspaceMock.mockResolvedValue({ projection: { items: [] } });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    if (platform) Object.defineProperty(window.navigator, "platform", platform);
    else delete (window.navigator as { platform?: string }).platform;
  });

  it("fetches only the row the highlight rests on, not every row it passes", async () => {
    renderRail("a");
    await screen.findAllByTestId("session-rail-row");
    vi.useFakeTimers({ shouldAdvanceTime: false });
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByLabelText("Filter sessions");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    const previewed = fetchWorkspaceMock.mock.calls.map(([id]) => id);
    // The first row previews on open (normally the open, already cached
    // session); rows the arrows only passed over are never fetched.
    expect(previewed).toContain("d");
    expect(previewed).not.toContain("b");
    expect(previewed).not.toContain("c");
  });
});

describe("session switcher focus", () => {
  const platform = Object.getOwnPropertyDescriptor(window.navigator, "platform");

  beforeEach(() => {
    Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
    fetchAgentSessionsMock.mockResolvedValue(list(["a", "b"]));
    fetchWorkspaceMock.mockResolvedValue({ projection: { items: [] } });
  });

  afterEach(() => {
    vi.clearAllMocks();
    if (platform) Object.defineProperty(window.navigator, "platform", platform);
    else delete (window.navigator as { platform?: string }).platform;
  });

  it("returns focus to where it was when the switcher closes", async () => {
    const view = renderRail("a");
    await screen.findAllByTestId("session-rail-row");
    const composer = document.createElement("textarea");
    view.container.appendChild(composer);
    composer.focus();

    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const field = screen.getByLabelText("Filter sessions");
    expect(field).toHaveFocus();
    fireEvent.keyDown(field, { key: "Escape" });
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByTestId("session-switcher")).not.toBeInTheDocument();
    expect(composer).toHaveFocus();
  });
});

describe("trimPreviewMarkdown", () => {
  it("cuts at a block boundary, never mid-heading or mid-list", () => {
    const text = `## Where things stand\n\nFirst paragraph.\n\n- **Task generator:** ${"x".repeat(80)}`;
    expect(trimPreviewMarkdown(text, 60)).toBe("## Where things stand\n\nFirst paragraph.…");
    expect(trimPreviewMarkdown("short", 60)).toBe("short");
    expect(trimPreviewMarkdown("y".repeat(100), 60)).toBe(`${"y".repeat(60)}…`);
  });

  it("never leaves a code fence open", () => {
    const fence = "`".repeat(3);
    const text = `Intro line here.\n\n${fence}sh\necho one\n\necho two\n${"z".repeat(80)}\n${fence}`;
    expect(trimPreviewMarkdown(text, 50)).toBe("Intro line here.…");
    // An inline triple-backtick after the opener does not fool the cut.
    const inline = `Intro line here.\n\n${fence}sh\nrun ${fence}x${fence} now\n\necho two\n${"z".repeat(80)}`;
    expect(trimPreviewMarkdown(inline, 60)).toBe("Intro line here.…");
    // Indented and tilde fences count too.
    const tilde = `Intro line here.\n\n  ~~~\necho one\n\necho two\n${"z".repeat(80)}`;
    expect(trimPreviewMarkdown(tilde, 50)).toBe("Intro line here.…");
    // A fence on the first line is closed, not cut to nothing.
    const first = `${fence}sh\necho one\n\necho two\n${"z".repeat(80)}`;
    expect(trimPreviewMarkdown(first, 40)).toBe(`${fence}sh\necho one\n…\n${fence}`);
  });
});
