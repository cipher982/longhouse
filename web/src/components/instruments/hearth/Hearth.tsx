/**
 * Hearth: each session's status as a small fire driven by its activity.
 *
 *   <HearthProvider>   one shared WebGL2 canvas for a list of rows
 *     <HearthLamp/>    a fire cell (a DOM placeholder the renderer draws
 *                      into) plus the state's text label
 *
 * The renderer module (renderer.ts + shaders.ts) loads on demand. Without
 * WebGL2 float render targets, after a lost context, or outside a provider,
 * every cell draws a static SVG coal-and-flame glyph for its state instead.
 * Styles: the ".hearth-lamp" block of styles/instruments.css.
 */

import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { StatusLampState } from "../StatusLamp";
import { T_AMB, initialBed, snapshotSignature, type HearthSnapshot } from "./signals";
import type { HearthRenderer, HearthStats } from "./renderer";

type HearthStatus = "pending" | "gl" | "fallback";

interface HearthApi {
  status: HearthStatus;
  register(id: string, key: string, el: HTMLElement, onStarved: (starved: boolean) => void): void;
  update(id: string, snap: HearthSnapshot): void;
  unregister(id: string): void;
  touch(): void;
}

const HearthContext = createContext<HearthApi | null>(null);

declare global {
  interface Window {
    /** Live renderer stats for perf checks: `__longhouseHearth?.()`. */
    __longhouseHearth?: () => HearthStats | null;
  }
}

function webgl2Available(): boolean {
  return typeof window !== "undefined" && typeof WebGL2RenderingContext !== "undefined";
}

export function HearthProvider({ children }: { children: ReactNode }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<HearthRenderer | null>(null);
  const cellsRef = useRef(
    new Map<string, { key: string; el: HTMLElement; snap: HearthSnapshot | null; onStarved: (starved: boolean) => void }>(),
  );
  const [status, setStatus] = useState<HearthStatus>(() => (webgl2Available() ? "pending" : "fallback"));

  useEffect(() => {
    if (!webgl2Available()) return;
    let cancelled = false;
    import("./renderer")
      .then(({ HearthRenderer }) => {
        const canvas = canvasRef.current;
        if (cancelled || !canvas) return;
        const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
        const renderer = HearthRenderer.create(canvas, {
          reducedMotion,
          onLost: () => {
            rendererRef.current = null;
            setStatus("fallback");
          },
        });
        if (!renderer) {
          setStatus("fallback");
          return;
        }
        rendererRef.current = renderer;
        for (const [id, cell] of cellsRef.current) {
          renderer.register(id, cell.key, cell.el, cell.onStarved);
          if (cell.snap) renderer.update(id, cell.snap);
        }
        window.__longhouseHearth = () => rendererRef.current?.getStats() ?? null;
        setStatus("gl");
      })
      .catch(() => {
        if (!cancelled) setStatus("fallback");
      });
    return () => {
      cancelled = true;
      rendererRef.current?.destroy();
      rendererRef.current = null;
      delete window.__longhouseHearth;
    };
  }, []);

  const register = useCallback((id: string, key: string, el: HTMLElement, onStarved: (starved: boolean) => void) => {
    cellsRef.current.set(id, { key, el, snap: null, onStarved });
    rendererRef.current?.register(id, key, el, onStarved);
  }, []);
  const update = useCallback((id: string, snap: HearthSnapshot) => {
    const cell = cellsRef.current.get(id);
    if (cell) cell.snap = snap;
    rendererRef.current?.update(id, snap);
  }, []);
  const unregister = useCallback((id: string) => {
    cellsRef.current.delete(id);
    rendererRef.current?.unregister(id);
  }, []);
  const touch = useCallback(() => rendererRef.current?.touch(), []);

  const api = useMemo<HearthApi>(() => ({ status, register, update, unregister, touch }), [status, register, update, unregister, touch]);

  return (
    <HearthContext.Provider value={api}>
      {/* The canvas lives in the scrolling content beside the rows, so the
          compositor moves both together (momentum, elastic overscroll). */}
      <div className="hearth-host">
        {children}
        {status !== "fallback" ? <canvas ref={canvasRef} className="hearth-canvas" aria-hidden="true" /> : null}
      </div>
    </HearthContext.Provider>
  );
}

export function HearthLamp({
  sessionKey,
  snapshot,
  state,
  label,
  title,
}: {
  /** Heat is kept per session, so a row that remounts keeps its fire. */
  sessionKey: string;
  snapshot: HearthSnapshot;
  /** The row's lamp state; styles the label (working amber, waiting ember, ...). */
  state: StatusLampState;
  label: string;
  title?: string;
}) {
  const ctx = useContext(HearthContext);
  const cellRef = useRef<HTMLSpanElement>(null);
  const id = useId();
  const register = ctx?.register;
  const unregister = ctx?.unregister;
  const update = ctx?.update;
  const touch = ctx?.touch;
  const signature = snapshotSignature(snapshot);
  const snapRef = useRef(snapshot);
  snapRef.current = snapshot;
  // On screen but the atlas is full: this row draws its static glyph.
  const [starved, setStarved] = useState(false);

  useLayoutEffect(() => {
    const el = cellRef.current;
    if (!register || !unregister || !el) return;
    register(id, sessionKey, el, setStarved);
    return () => {
      unregister(id);
      setStarved(false);
    };
  }, [register, unregister, id, sessionKey]);

  useEffect(() => {
    update?.(id, snapRef.current);
  }, [update, id, sessionKey, signature]);

  // Any re-render may have moved the row (a drag transform, a reflow).
  useLayoutEffect(() => {
    touch?.();
  });

  const glyph = !ctx || ctx.status === "fallback" || starved;
  // The fire is decoration: the visible label beside it carries the state.
  return (
    <span className="hearth-lamp" data-state={state} data-mode={snapshot.mode} title={title ?? label}>
      <span ref={cellRef} className="hearth-lamp__cell" aria-hidden="true" data-hearth={glyph ? "glyph" : "gl"}>
        {glyph ? <HearthGlyph snapshot={snapshot} /> : null}
      </span>
      <span className="hearth-lamp__label">{label}</span>
    </span>
  );
}

/** Coal colour for a bed temperature, matching the blackbody ramp's reading. */
export function coalColor(kelvin: number): string {
  const stops: [number, [number, number, number]][] = [
    [T_AMB, [30, 24, 20]],
    [600, [38, 26, 20]],
    [720, [92, 26, 12]],
    [850, [158, 42, 14]],
    [1000, [218, 86, 26]],
    [1150, [245, 142, 44]],
  ];
  let lo = stops[0];
  let hi = stops[stops.length - 1];
  if (kelvin <= lo[0]) return `rgb(${lo[1].join(",")})`;
  if (kelvin >= hi[0]) return `rgb(${hi[1].join(",")})`;
  for (let i = 1; i < stops.length; i++) {
    if (kelvin <= stops[i][0]) {
      lo = stops[i - 1];
      hi = stops[i];
      break;
    }
  }
  const f = (kelvin - lo[0]) / (hi[0] - lo[0]);
  const c = lo[1].map((v, i) => Math.round(v + (hi[1][i] - v) * f));
  return `rgb(${c.join(",")})`;
}

/** Static fallback, drawn like the live fire: no box, just a low coal pile
 * on the baseline coloured by its cooled temperature, a flame for a working
 * session (with a faint warm spill) and a low one while it waits on you.
 * Ended is a small grey smudge of ash. */
function HearthGlyph({ snapshot }: { snapshot: HearthSnapshot }) {
  const uid = useId();
  const flameId = `${uid}-flame`;
  const spillId = `${uid}-spill`;
  const bed = initialBed(snapshot, Date.now());
  const mode = snapshot.mode;
  const flame =
    mode === "working"
      ? "M13 7 C17 13 20 18 19 23 C18.5 27.5 16 30 13 30 C10 30 7.5 27.5 7 23 C6.5 18.5 10 15 11 11 C11.8 14 12.8 15 13.6 16 C14.2 13 13.8 10 13 7 Z"
      : mode === "waiting"
        ? "M13 17 C15.6 20 17 22.5 16.6 25.5 C16.2 28.4 14.8 30 13 30 C11.2 30 9.8 28.4 9.4 25.5 C9.2 23.4 10.6 21.6 11.6 20.2 C12.2 21.4 12.8 22 13.3 22.4 C13.7 20.8 13.6 19 13 17 Z"
        : null;
  if (mode === "ended") {
    return (
      <svg className="hearth-glyph" viewBox="0 0 26 36" aria-hidden="true" focusable="false">
        <ellipse cx="11.5" cy="33.6" rx="3.6" ry="1.3" fill="rgb(96,91,85)" opacity="0.8" />
        <ellipse cx="15" cy="33.9" rx="2.6" ry="1" fill="rgb(120,114,107)" opacity="0.7" />
      </svg>
    );
  }
  const coal = coalColor(bed.surface);
  const crack = coalColor(Math.max(bed.surface, bed.core));
  const crackOpacity = Math.min(1, Math.max(0.25, (bed.core - 450) / 500));
  return (
    <svg className="hearth-glyph" viewBox="0 0 26 36" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={flameId} x1="0" y1="1" x2="0" y2="0">
          <stop offset="0" stopColor="#fff1c4" />
          <stop offset="0.35" stopColor="#ffb44a" />
          <stop offset="0.75" stopColor="#f0621c" />
          <stop offset="1" stopColor="#a8260e" stopOpacity="0" />
        </linearGradient>
        <radialGradient id={spillId} cx="0.5" cy="0.7" r="0.5">
          <stop offset="0" stopColor="#f08a24" stopOpacity="0.22" />
          <stop offset="1" stopColor="#f08a24" stopOpacity="0" />
        </radialGradient>
      </defs>
      {flame ? <ellipse cx="13" cy="26" rx="13" ry="10" fill={`url(#${spillId})`} /> : null}
      {flame ? <path d={flame} fill={`url(#${flameId})`} /> : null}
      <ellipse cx="9.6" cy="32.8" rx="3.2" ry="1.7" fill={coal} />
      <ellipse cx="16.2" cy="32.9" rx="3" ry="1.6" fill={coal} />
      <ellipse cx="13" cy="31.6" rx="3.4" ry="2" fill={coal} />
      <path d="M10 33.2 L12.4 32.4 L13.4 33 L15.8 32.2" stroke={crack} strokeWidth="0.6" strokeLinecap="round" fill="none" opacity={crackOpacity} />
    </svg>
  );
}
