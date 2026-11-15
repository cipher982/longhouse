import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useTurnSeek } from "../useTurnSeek";

describe("useTurnSeek", () => {
  it("scrolls at once when the turn's row is already on the page", () => {
    const scrollToTurn = vi.fn(() => true);
    const fetchPreviousPage = vi.fn();
    const { result } = renderHook(() =>
      useTurnSeek({ events: [], hasPreviousPage: true, isFetchingPreviousPage: false, fetchPreviousPage, scrollToTurn }),
    );
    act(() => result.current.seek("u1"));
    expect(scrollToTurn).toHaveBeenCalledWith("u1");
    expect(fetchPreviousPage).not.toHaveBeenCalled();
    expect(result.current.seeking).toBeNull();
  });

  it("pages back until the event loads, then scrolls and lets go of it", async () => {
    let rowExists = false;
    const scrollToTurn = vi.fn(() => rowExists);
    const fetchPreviousPage = vi.fn();
    const { result, rerender } = renderHook(
      ({ events, fetching }: { events: { id: string }[]; fetching: boolean }) =>
        useTurnSeek({ events, hasPreviousPage: true, isFetchingPreviousPage: fetching, fetchPreviousPage, scrollToTurn }),
      { initialProps: { events: [{ id: "u9" }], fetching: false } },
    );

    act(() => result.current.seek("u1"));
    expect(fetchPreviousPage).toHaveBeenCalledTimes(1);
    expect(result.current.seeking).toBe("u1");

    rerender({ events: [{ id: "u9" }], fetching: true });
    expect(fetchPreviousPage).toHaveBeenCalledTimes(1);

    rowExists = true;
    rerender({ events: [{ id: "u1" }, { id: "u9" }], fetching: false });
    await act(async () => {
      await new Promise((resolve) => window.requestAnimationFrame(() => resolve(null)));
    });
    expect(scrollToTurn).toHaveBeenLastCalledWith("u1");
    expect(result.current.seeking).toBeNull();
    expect(fetchPreviousPage).toHaveBeenCalledTimes(1);
  });

  it("gives up when the whole session is loaded and the event is not in it", () => {
    const fetchPreviousPage = vi.fn();
    const { result } = renderHook(() =>
      useTurnSeek({
        events: [{ id: "u9" }],
        hasPreviousPage: false,
        isFetchingPreviousPage: false,
        fetchPreviousPage,
        scrollToTurn: () => false,
      }),
    );
    act(() => result.current.seek("gone"));
    expect(fetchPreviousPage).not.toHaveBeenCalled();
    expect(result.current.seeking).toBeNull();
  });
});
