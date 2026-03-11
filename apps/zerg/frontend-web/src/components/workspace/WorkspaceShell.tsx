import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

const WORKSPACE_LAYOUT_STORAGE_KEY = "zerg:session-workspace-layout:v1";
const SIDEBAR_DEFAULT_WIDTH = 248;
const SIDEBAR_MIN_WIDTH = 220;
const SIDEBAR_MAX_WIDTH = 420;
const INSPECTOR_DEFAULT_WIDTH = 336;
const INSPECTOR_MIN_WIDTH = 280;
const INSPECTOR_MAX_WIDTH = 520;
const MIN_MAIN_WIDTH = 560;

type WorkspaceLayout = {
  sidebarWidth: number;
  inspectorWidth: number;
};

type ResizePane = "sidebar" | "inspector";

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function getGridGapPx(element: HTMLElement): number {
  const styles = window.getComputedStyle(element);
  const parsed = parseFloat(styles.columnGap || styles.gap || "16");
  return Number.isFinite(parsed) ? parsed : 16;
}

function sameLayout(a: WorkspaceLayout, b: WorkspaceLayout): boolean {
  return a.sidebarWidth === b.sidebarWidth && a.inspectorWidth === b.inspectorWidth;
}

function readStoredLayout(): WorkspaceLayout {
  const defaults = {
    sidebarWidth: SIDEBAR_DEFAULT_WIDTH,
    inspectorWidth: INSPECTOR_DEFAULT_WIDTH,
  };

  if (typeof window === "undefined" || typeof localStorage === "undefined") {
    return defaults;
  }

  try {
    const raw = localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY);
    if (!raw) return defaults;

    const parsed = JSON.parse(raw) as Partial<WorkspaceLayout> | null;
    return {
      sidebarWidth:
        typeof parsed?.sidebarWidth === "number"
          ? clamp(parsed.sidebarWidth, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH)
          : defaults.sidebarWidth,
      inspectorWidth:
        typeof parsed?.inspectorWidth === "number"
          ? clamp(parsed.inspectorWidth, INSPECTOR_MIN_WIDTH, INSPECTOR_MAX_WIDTH)
          : defaults.inspectorWidth,
    };
  } catch {
    return defaults;
  }
}

function clampSidebarWidth(
  requestedWidth: number,
  containerWidth: number,
  gapPx: number,
  inspectorWidth: number,
  hasInspector: boolean,
): number {
  const maxSidebar = hasInspector
    ? Math.min(
        SIDEBAR_MAX_WIDTH,
        Math.max(SIDEBAR_MIN_WIDTH, containerWidth - MIN_MAIN_WIDTH - inspectorWidth - gapPx * 2),
      )
    : Math.min(
        SIDEBAR_MAX_WIDTH,
        Math.max(SIDEBAR_MIN_WIDTH, containerWidth - MIN_MAIN_WIDTH - gapPx),
      );
  return clamp(requestedWidth, SIDEBAR_MIN_WIDTH, maxSidebar);
}

function clampInspectorWidth(
  requestedWidth: number,
  containerWidth: number,
  gapPx: number,
  sidebarWidth: number,
): number {
  const maxInspector = Math.min(
    INSPECTOR_MAX_WIDTH,
    Math.max(INSPECTOR_MIN_WIDTH, containerWidth - MIN_MAIN_WIDTH - sidebarWidth - gapPx * 2),
  );
  return clamp(requestedWidth, INSPECTOR_MIN_WIDTH, maxInspector);
}

function clampLayout(
  layout: WorkspaceLayout,
  containerWidth: number,
  gapPx: number,
  hasInspector: boolean,
  preferredPane: ResizePane | null = null,
): WorkspaceLayout {
  let sidebarWidth = clamp(layout.sidebarWidth, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH);
  let inspectorWidth = clamp(layout.inspectorWidth, INSPECTOR_MIN_WIDTH, INSPECTOR_MAX_WIDTH);

  if (containerWidth <= 0) {
    return { sidebarWidth, inspectorWidth };
  }

  if (!hasInspector) {
    sidebarWidth = clampSidebarWidth(sidebarWidth, containerWidth, gapPx, 0, false);
    return { sidebarWidth, inspectorWidth };
  }

  if (preferredPane === "sidebar") {
    sidebarWidth = clampSidebarWidth(sidebarWidth, containerWidth, gapPx, inspectorWidth, true);
    return { sidebarWidth, inspectorWidth };
  }

  if (preferredPane === "inspector") {
    inspectorWidth = clampInspectorWidth(inspectorWidth, containerWidth, gapPx, sidebarWidth);
    return { sidebarWidth, inspectorWidth };
  }

  inspectorWidth = clampInspectorWidth(inspectorWidth, containerWidth, gapPx, sidebarWidth);
  sidebarWidth = clampSidebarWidth(sidebarWidth, containerWidth, gapPx, inspectorWidth, true);

  return { sidebarWidth, inspectorWidth };
}

interface WorkspaceShellProps {
  header: ReactNode;
  sidebar: ReactNode;
  main: ReactNode;
  inspector?: ReactNode;
  bottom?: ReactNode;
}

export function WorkspaceShell({
  header,
  sidebar,
  main,
  inspector,
  bottom,
}: WorkspaceShellProps) {
  const hasInspector = Boolean(inspector);
  const hasBottom = Boolean(bottom);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const layoutRef = useRef<WorkspaceLayout>(readStoredLayout());
  const [layout, setLayout] = useState<WorkspaceLayout>(layoutRef.current);
  const [activePane, setActivePane] = useState<ResizePane | null>(null);

  useEffect(() => {
    layoutRef.current = layout;
  }, [layout]);

  useEffect(() => {
    if (typeof localStorage === "undefined") return;
    try {
      localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, JSON.stringify(layout));
    } catch {
      // Ignore persistence failures; resizing should still work for this session.
    }
  }, [layout]);

  const commitLayout = useCallback(
    (candidate: WorkspaceLayout, preferredPane: ResizePane | null = null) => {
      const body = bodyRef.current;
      if (!body) return;

      const containerWidth = body.getBoundingClientRect().width;
      const gapPx = getGridGapPx(body);
      const next = clampLayout(candidate, containerWidth, gapPx, hasInspector, preferredPane);

      if (!sameLayout(next, layoutRef.current)) {
        setLayout(next);
      }
    },
    [hasInspector],
  );

  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;

    const syncLayout = () => {
      const containerWidth = body.getBoundingClientRect().width;
      const gapPx = getGridGapPx(body);
      const next = clampLayout(layoutRef.current, containerWidth, gapPx, hasInspector);
      if (!sameLayout(next, layoutRef.current)) {
        setLayout(next);
      }
    };

    syncLayout();

    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(syncLayout);
      observer.observe(body);
      return () => observer.disconnect();
    }

    window.addEventListener("resize", syncLayout);
    return () => window.removeEventListener("resize", syncLayout);
  }, [hasInspector]);

  const resizeFromPointer = useCallback(
    (pane: ResizePane, clientX: number) => {
      const body = bodyRef.current;
      if (!body) return;

      const rect = body.getBoundingClientRect();
      const gapPx = getGridGapPx(body);
      const nextWidth =
        pane === "sidebar"
          ? clientX - rect.left - gapPx / 2
          : rect.right - clientX - gapPx / 2;

      commitLayout({
        ...layoutRef.current,
        ...(pane === "sidebar"
          ? { sidebarWidth: nextWidth }
          : { inspectorWidth: nextWidth }),
      }, pane);
    },
    [commitLayout],
  );

  useEffect(() => {
    if (!activePane) return;

    const handlePointerMove = (event: PointerEvent) => {
      resizeFromPointer(activePane, event.clientX);
    };

    const stopResize = () => {
      setActivePane(null);
      document.body.style.userSelect = "";
      document.documentElement.style.cursor = "";
    };

    document.body.style.userSelect = "none";
    document.documentElement.style.cursor = "col-resize";

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
      document.body.style.userSelect = "";
      document.documentElement.style.cursor = "";
    };
  }, [activePane, resizeFromPointer]);

  const startResize = (pane: ResizePane) => (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    setActivePane(pane);
    resizeFromPointer(pane, event.clientX);
  };

  const nudgePane = useCallback(
    (pane: ResizePane, delta: number) => {
      commitLayout({
        ...layoutRef.current,
        ...(pane === "sidebar"
          ? { sidebarWidth: layoutRef.current.sidebarWidth + delta }
          : { inspectorWidth: layoutRef.current.inspectorWidth + delta }),
      }, pane);
    },
    [commitLayout],
  );

  const resetPane = useCallback(
    (pane: ResizePane) => {
      commitLayout({
        ...layoutRef.current,
        ...(pane === "sidebar"
          ? { sidebarWidth: SIDEBAR_DEFAULT_WIDTH }
          : { inspectorWidth: INSPECTOR_DEFAULT_WIDTH }),
      }, pane);
    },
    [commitLayout],
  );

  const handleResizeKeyDown =
    (pane: ResizePane) => (event: KeyboardEvent<HTMLDivElement>) => {
      const step = event.shiftKey ? 64 : 32;

      if (event.key === "ArrowLeft") {
        event.preventDefault();
        nudgePane(pane, pane === "sidebar" ? -step : step);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        nudgePane(pane, pane === "sidebar" ? step : -step);
      } else if (event.key === "Home") {
        event.preventDefault();
        commitLayout({
          ...layoutRef.current,
          ...(pane === "sidebar"
            ? { sidebarWidth: SIDEBAR_MIN_WIDTH }
            : { inspectorWidth: INSPECTOR_MIN_WIDTH }),
        }, pane);
      } else if (event.key === "End") {
        event.preventDefault();
        commitLayout({
          ...layoutRef.current,
          ...(pane === "sidebar"
            ? { sidebarWidth: SIDEBAR_MAX_WIDTH }
            : { inspectorWidth: INSPECTOR_MAX_WIDTH }),
        }, pane);
      } else if (event.key === "Enter") {
        event.preventDefault();
        resetPane(pane);
      }
    };

  const shellStyle = {
    "--workspace-sidebar-width": `${layout.sidebarWidth}px`,
    "--workspace-inspector-width": `${layout.inspectorWidth}px`,
  } as CSSProperties;

  return (
    <div
      className={`workspace-shell${hasInspector ? "" : " workspace-shell--inspector-collapsed"}${hasBottom ? " workspace-shell--with-bottom" : ""}${activePane ? " workspace-shell--is-resizing" : ""}`}
      style={shellStyle}
    >
      <div className="workspace-shell__header">{header}</div>
      <div className="workspace-shell__body" ref={bodyRef}>
        <aside className="workspace-shell__pane workspace-shell__pane--sidebar">{sidebar}</aside>
        <div
          role="separator"
          tabIndex={0}
          aria-label="Resize session sidebar"
          aria-orientation="vertical"
          aria-valuemin={SIDEBAR_MIN_WIDTH}
          aria-valuemax={SIDEBAR_MAX_WIDTH}
          aria-valuenow={Math.round(layout.sidebarWidth)}
          data-testid="session-workspace-sidebar-resize"
          title="Drag to resize the session sidebar. Press Enter or double-click to reset."
          className={`workspace-shell__resize-handle workspace-shell__resize-handle--sidebar${activePane === "sidebar" ? " is-active" : ""}`}
          onPointerDown={startResize("sidebar")}
          onDoubleClick={() => resetPane("sidebar")}
          onKeyDown={handleResizeKeyDown("sidebar")}
        />
        <main className="workspace-shell__pane workspace-shell__pane--main">{main}</main>
        {hasInspector ? (
          <div
            role="separator"
            tabIndex={0}
            aria-label="Resize event inspector"
            aria-orientation="vertical"
            aria-valuemin={INSPECTOR_MIN_WIDTH}
            aria-valuemax={INSPECTOR_MAX_WIDTH}
            aria-valuenow={Math.round(layout.inspectorWidth)}
            data-testid="session-workspace-inspector-resize"
            title="Drag to resize the event inspector. Press Enter or double-click to reset."
            className={`workspace-shell__resize-handle workspace-shell__resize-handle--inspector${activePane === "inspector" ? " is-active" : ""}`}
            onPointerDown={startResize("inspector")}
            onDoubleClick={() => resetPane("inspector")}
            onKeyDown={handleResizeKeyDown("inspector")}
          />
        ) : null}
        {hasInspector ? (
          <aside className="workspace-shell__pane workspace-shell__pane--inspector">{inspector}</aside>
        ) : null}
      </div>
      {bottom ? <div className="workspace-shell__bottom">{bottom}</div> : null}
    </div>
  );
}
