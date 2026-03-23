import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useBodyScrollLock } from "../useBodyScrollLock";
import { useClickOutside } from "../useClickOutside";
import { useEscapeKey } from "../useEscapeKey";

function ClickOutsideHarness({
  enabled = true,
  onClickOutside,
}: {
  enabled?: boolean;
  onClickOutside: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const anchorRef = useRef<HTMLButtonElement>(null);

  useClickOutside({
    enabled,
    refs: [panelRef, anchorRef],
    onClickOutside,
  });

  return (
    <div>
      <div ref={panelRef}>inside</div>
      <button ref={anchorRef} type="button">
        anchor
      </button>
      <button type="button">outside</button>
    </div>
  );
}

describe("interaction hooks", () => {
  afterEach(() => {
    document.body.style.overflow = "";
    vi.restoreAllMocks();
  });

  it("locks body scroll and restores the previous overflow value", () => {
    document.body.style.overflow = "clip";

    const { rerender, unmount } = renderHook(
      ({ locked }) => useBodyScrollLock(locked),
      { initialProps: { locked: false } },
    );

    rerender({ locked: true });
    expect(document.body.style.overflow).toBe("hidden");

    rerender({ locked: false });
    expect(document.body.style.overflow).toBe("clip");

    rerender({ locked: true });
    unmount();
    expect(document.body.style.overflow).toBe("clip");
  });

  it("fires the escape handler only when enabled", () => {
    const onEscape = vi.fn();

    const { rerender } = renderHook(
      ({ enabled }) => useEscapeKey(onEscape, enabled),
      { initialProps: { enabled: true } },
    );

    fireEvent.keyDown(document, { key: "Enter" });
    expect(onEscape).not.toHaveBeenCalled();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onEscape).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("ignores inside refs and only fires for outside clicks", () => {
    const onClickOutside = vi.fn();

    render(<ClickOutsideHarness onClickOutside={onClickOutside} />);

    fireEvent.mouseDown(screen.getByText("inside"));
    fireEvent.mouseDown(screen.getByText("anchor"));
    expect(onClickOutside).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByText("outside"));
    expect(onClickOutside).toHaveBeenCalledTimes(1);
  });

  it("does not fire outside clicks when disabled", () => {
    const onClickOutside = vi.fn();

    render(<ClickOutsideHarness enabled={false} onClickOutside={onClickOutside} />);

    fireEvent.mouseDown(screen.getByText("outside"));
    expect(onClickOutside).not.toHaveBeenCalled();
  });
});
