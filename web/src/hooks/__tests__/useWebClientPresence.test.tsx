import { render, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWebClientPresence } from "../useWebClientPresence";
import { postWebClientPresence } from "../../services/api/clientPresence";

vi.mock("../../lib/config", () => {
  const config = { authEnabled: true, demoMode: false };
  return { default: config, config };
});

vi.mock("../../services/api/clientPresence", () => ({
  postWebClientPresence: vi.fn(() =>
    Promise.resolve({
      client_id: "stored-client",
      client_type: "web",
      visible: true,
      route: "/timeline/session-1",
      session_id: "session-1",
      last_seen_at: new Date(0).toISOString(),
    }),
  ),
}));

function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: hidden,
  });
}

function Harness() {
  useWebClientPresence();
  return null;
}

describe("useWebClientPresence", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
    window.localStorage.setItem("longhouse.webClientId", "stored-client");
    setDocumentHidden(false);
    vi.mocked(postWebClientPresence).mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("heartbeats current web visibility and route context", async () => {
    render(
      <MemoryRouter initialEntries={["/timeline/session-1?tab=detail"]}>
        <Harness />
      </MemoryRouter>,
    );

    await act(async () => {});

    expect(postWebClientPresence).toHaveBeenCalledWith({
      client_id: "stored-client",
      client_type: "web",
      visible: true,
      route: "/timeline/session-1?tab=detail",
      session_id: "session-1",
    });

    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    await act(async () => {});

    expect(postWebClientPresence).toHaveBeenCalledTimes(2);

    act(() => {
      setDocumentHidden(true);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(async () => {});

    expect(postWebClientPresence).toHaveBeenLastCalledWith({
      client_id: "stored-client",
      client_type: "web",
      visible: false,
      route: "/timeline/session-1?tab=detail",
      session_id: "session-1",
    });
  });
});
