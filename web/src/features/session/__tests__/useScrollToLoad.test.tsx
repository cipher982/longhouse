import { render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useScrollToLoad } from "../useScrollToLoad";

type ObserverCallback = (entries: Array<{ isIntersecting: boolean }>) => void;

function Harness({
  enabled,
  loading = false,
  onLoad,
  rootHeights,
}: {
  enabled: boolean;
  loading?: boolean;
  onLoad: () => void;
  rootHeights?: { clientHeight: number; scrollHeight: number };
}) {
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const attachRoot = (node: HTMLDivElement | null) => {
    rootRef.current = node;
    if (node && rootHeights) {
      Object.defineProperty(node, "clientHeight", {
        configurable: true,
        value: rootHeights.clientHeight,
      });
      Object.defineProperty(node, "scrollHeight", {
        configurable: true,
        value: rootHeights.scrollHeight,
      });
    }
  };

  useScrollToLoad({
    sentinelRef,
    rootRef,
    enabled,
    loading,
    onLoad,
  });

  return (
    <div ref={attachRoot}>
      <div ref={sentinelRef} />
    </div>
  );
}

describe("useScrollToLoad", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads immediately when the initial observer entry is already intersecting", () => {
    let callback: ObserverCallback | null = null;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(cb: ObserverCallback) {
          callback = cb;
        }
        observe() {}
        disconnect() {}
      },
    );

    const onLoad = vi.fn();
    render(<Harness enabled onLoad={onLoad} />);

    callback?.([{ isIntersecting: true }]);
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("skips the initial observer entry when the sentinel is off-screen until it enters", () => {
    let callback: ObserverCallback | null = null;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(cb: ObserverCallback) {
          callback = cb;
        }
        observe() {}
        disconnect() {}
      },
    );

    const onLoad = vi.fn();
    render(<Harness enabled onLoad={onLoad} />);

    callback?.([{ isIntersecting: false }]);
    expect(onLoad).not.toHaveBeenCalled();

    callback?.([{ isIntersecting: true }]);
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("auto-fills when the container does not overflow its viewport", () => {
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );

    const onLoad = vi.fn();
    render(
      <Harness
        enabled
        onLoad={onLoad}
        rootHeights={{ clientHeight: 800, scrollHeight: 40 }}
      />,
    );

    expect(onLoad).toHaveBeenCalledTimes(1);
  });
});
