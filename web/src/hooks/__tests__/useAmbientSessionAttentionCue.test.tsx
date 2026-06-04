import { render, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAmbientSessionAttentionCue } from "../useAmbientSessionAttentionCue";
import type { TimelineSessionCard } from "../../services/api/agents";

function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: hidden,
  });
}

function card(needsAttention: boolean): TimelineSessionCard {
  return {
    head: {
      runtime_display: {
        needs_attention: needsAttention,
      },
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
});
