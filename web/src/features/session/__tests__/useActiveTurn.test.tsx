import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { findActiveIndex, useActiveTurn } from "../useActiveTurn";

describe("findActiveIndex", () => {
  const tops = [-300, -40, 120, 500];
  const topAt = (i: number) => tops[i];

  it("picks the last item at or above the line", () => {
    expect(findActiveIndex(tops.length, topAt, 0)).toBe(1);
    expect(findActiveIndex(tops.length, topAt, 120)).toBe(2);
    expect(findActiveIndex(tops.length, topAt, 10_000)).toBe(3);
  });

  it("falls back to the first item above the first turn, and -1 when empty", () => {
    expect(findActiveIndex(tops.length, topAt, -1_000)).toBe(0);
    expect(findActiveIndex(0, topAt, 0)).toBe(-1);
  });
});

describe("useActiveTurn", () => {
  const VIEWPORT = 1000;
  // Row tops relative to the list's scroll origin; the list box sits at y=0.
  const ROW_OFFSETS = [0, 1500, 3000];
  let scrollTop = 0;
  let list: HTMLDivElement;
  let current: { activeKey: string | null; pin: (eventId: number) => void };

  function Probe() {
    current = useActiveTurn(list);
    return null;
  }

  async function frame() {
    const { promise, resolve } = Promise.withResolvers<void>();
    requestAnimationFrame(() => resolve());
    await act(async () => {
      await promise;
    });
  }

  async function scrollTo(top: number) {
    scrollTop = top;
    fireEvent.scroll(list);
    await frame();
  }

  /** Let the click's scroll be judged over (no scroll event for SETTLE_MS). */
  async function settled() {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
  }

  beforeEach(() => {
    scrollTop = 0;
    list = document.createElement("div");
    ROW_OFFSETS.forEach((offset, i) => {
      const row = document.createElement("div");
      row.id = `event-${i + 1}`;
      row.dataset.messageRole = "user";
      row.getBoundingClientRect = () => ({ top: offset - scrollTop, bottom: offset - scrollTop + 100 }) as DOMRect;
      list.appendChild(row);
    });
    // Assistant rows are never turns.
    const assistant = document.createElement("div");
    assistant.id = "event-99";
    assistant.dataset.messageRole = "assistant";
    list.appendChild(assistant);
    list.getBoundingClientRect = () => ({ top: 0, bottom: VIEWPORT, height: VIEWPORT }) as DOMRect;
    Object.defineProperty(list, "scrollTop", { get: () => scrollTop, configurable: true });
    Object.defineProperty(list, "clientHeight", { value: VIEWPORT, configurable: true });
    Object.defineProperty(list, "scrollHeight", { value: 4500, configurable: true });
    document.body.appendChild(list);
  });

  afterEach(() => {
    list.remove();
    vi.restoreAllMocks();
  });

  it("tracks the turn under the reference line as the reader scrolls", async () => {
    render(<Probe />);
    expect(current.activeKey).toBe("turn-1");

    await scrollTo(1400); // row 2 top at 100, inside the upper fifth
    expect(current.activeKey).toBe("turn-2");

    await scrollTo(1200); // row 2 top at 300, below the line: still turn 1
    expect(current.activeKey).toBe("turn-1");
  });

  it("marks the last turn once the reader reaches the bottom", async () => {
    render(<Probe />);
    await scrollTo(3500); // scrollHeight 4500 - 3500 - 1000 = 0
    expect(current.activeKey).toBe("turn-3");
  });

  it("holds a pinned turn through programmatic scrolling until the reader scrolls", async () => {
    render(<Probe />);
    act(() => current.pin(3));
    expect(current.activeKey).toBe("turn-3");

    await scrollTo(1400); // smooth-scroll frames on the way to the target
    expect(current.activeKey).toBe("turn-3");

    fireEvent.wheel(list);
    await scrollTo(1400);
    expect(current.activeKey).toBe("turn-2");
  });

  it("ignores scroll keys typed into the composer but honours them elsewhere", async () => {
    const input = document.createElement("textarea");
    document.body.appendChild(input);
    render(<Probe />);
    act(() => current.pin(3));

    fireEvent.keyDown(input, { key: " " });
    await scrollTo(1400);
    expect(current.activeKey).toBe("turn-3");

    fireEvent.keyDown(document.body, { key: "PageDown" });
    await scrollTo(1400);
    expect(current.activeKey).toBe("turn-2");
    input.remove();
  });

  it("drops a pin whose row a filter removed", async () => {
    render(<Probe />);
    act(() => current.pin(3));
    expect(current.activeKey).toBe("turn-3");

    list.querySelector("#event-3")?.remove();
    await frame(); // the mutation observer schedules a re-measure
    expect(current.activeKey).toBe("turn-1"); // scrollTop is 0, so measured by position
  });

  it("holds a settled pin while the list stays put, and ends it when anything moves the list (send, new-message pill)", async () => {
    render(<Probe />);
    act(() => current.pin(3));
    await scrollTo(3000); // the click's scroll arrives: row 3 at the top
    await settled();
    expect(current.activeKey).toBe("turn-3");

    await scrollTo(0); // something else scrolled the list away
    expect(current.activeKey).toBe("turn-1");
  });

  it("ends the pin of a click that scrolled nothing when the list later jumps", async () => {
    render(<Probe />);
    await scrollTo(3000); // row 3 already at the top
    act(() => current.pin(3)); // no scroll follows the click
    await settled();

    await scrollTo(0);
    expect(current.activeKey).toBe("turn-1");
  });

  it("ends the pin when the click's scroll came to rest away from its row", async () => {
    render(<Probe />);
    act(() => current.pin(3));
    await scrollTo(100); // a send or the new-message pill overtook the click
    await settled();
    expect(current.activeKey).toBe("turn-1");
  });

  it("ends a settled pin when a layout shift carries its row off screen without any scroll", async () => {
    render(<Probe />);
    act(() => current.pin(3));
    await scrollTo(3000);
    await settled();
    expect(current.activeKey).toBe("turn-3");

    const row = list.querySelector<HTMLElement>("#event-3")!;
    row.getBoundingClientRect = () => ({ top: 5000, bottom: 5100 }) as DOMRect; // content above grew
    list.appendChild(document.createElement("div")); // any DOM change re-measures
    await frame();
    expect(current.activeKey).toBe("turn-2");
  });

  it("ignores Space that activates a transcript button instead of scrolling", async () => {
    const button = document.createElement("button");
    list.appendChild(button);
    render(<Probe />);
    act(() => current.pin(3));
    await scrollTo(3000);
    await settled();

    fireEvent.keyDown(button, { key: " " });
    await frame();
    expect(current.activeKey).toBe("turn-3");
  });
});
