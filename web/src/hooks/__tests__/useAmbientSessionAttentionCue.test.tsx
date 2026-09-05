import { render, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAmbientSessionAttentionCue } from "../useAmbientSessionAttentionCue";
import type { TimelineSessionCard } from "../../services/api/agents";
import { makeSessionStateFacts } from "../../test/sessionState";

function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: hidden,
  });
}

function card(
  needsAttention: boolean,
  options: { closed?: boolean; userState?: string } = {},
): TimelineSessionCard {
  return {
    head: {
      user_state: options.userState,
      session_state: makeSessionStateFacts({ pendingInteraction: needsAttention, closed: options.closed }),
    },
  } as TimelineSessionCard;
}

function Harness({ sessions }: { sessions: TimelineSessionCard[] }) {
  useAmbientSessionAttentionCue(sessions);
  return null;
}

describe("useAmbientSessionAttentionCue", () => {
  const setAppBadge = vi.fn(() => Promise.resolve());
  const clearAppBadge = vi.fn(() => Promise.resolve());

  beforeEach(() => {
    const title = document.querySelector("title") ?? document.head.appendChild(document.createElement("title"));
    title.textContent = "Longhouse";
    const icon = document.createElement("link");
    icon.rel = "icon";
    icon.href = "/favicon-32.png?v=3";
    document.head.appendChild(icon);
    setDocumentHidden(true);
    setAppBadge.mockClear();
    clearAppBadge.mockClear();
    Object.defineProperty(navigator, "setAppBadge", {
      configurable: true,
      value: setAppBadge,
    });
    Object.defineProperty(navigator, "clearAppBadge", {
      configurable: true,
      value: clearAppBadge,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("marks hidden tabs when timeline sessions need attention and clears on return", async () => {
    render(<Harness sessions={[card(true), card(false)]} />);

    await waitFor(() => {
      expect(document.title).toBe("● Blocked · Longhouse");
      expect(setAppBadge).toHaveBeenCalledWith(1);
      expect(document.querySelector('link[rel="icon"]')?.getAttribute("href")).toContain("attention=1");
    });

    act(() => {
      setDocumentHidden(false);
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => {
      expect(document.title).toBe("Longhouse");
      expect(clearAppBadge).toHaveBeenCalled();
    });
  });

  it("removes closed and inactive pending interactions from hidden-tab attention cues", async () => {
    const { rerender } = render(<Harness sessions={[card(true), card(true)]} />);
    await waitFor(() => {
      expect(setAppBadge).toHaveBeenLastCalledWith(2);
      expect(document.title).toBe("● 2 blocked · Longhouse");
    });

    rerender(<Harness sessions={[card(true, { closed: true }), card(true)]} />);
    await waitFor(() => {
      expect(setAppBadge).toHaveBeenLastCalledWith(1);
      expect(document.title).toBe("● Blocked · Longhouse");
    });

    clearAppBadge.mockClear();
    rerender(<Harness sessions={[card(true, { closed: true }), card(true, { userState: "parked" })]} />);
    await waitFor(() => {
      expect(clearAppBadge).toHaveBeenCalled();
      expect(document.title).toBe("Longhouse");
      expect(document.querySelector('link[rel="icon"]')?.getAttribute("href")).not.toContain("attention=1");
    });
  });
});
