/**
 * HearthRenderer: one WebGL2 context for every fire in the timeline list.
 *
 * Each row's fire cell is a DOM placeholder; the renderer reads its rect and
 * composites that row's tile of a shared simulation atlas into it. Only
 * visible cells hold a tile (capacity-capped), tiles with no flame drop out
 * of the solver, and the frame loop stops entirely when nothing is burning,
 * the tab is hidden, or no cell is on screen. Under reduced motion it draws a
 * warmed still frame per change and never loops.
 *
 * The canvas is fixed-position and sized to the strip the visible cells
 * occupy, not the viewport, so an idle list costs the compositor nothing.
 */

import { NP, NS, PH, PW, SLOT_X, TH, TW, buildBlackbodyLUT, hearthShaders } from "./shaders";
import { SessionHeat, T_AMB, kindOf, type HearthEvent, type HearthSnapshot, type ToolKind } from "./signals";

const JACOBI = 20;
const H = 1 / 60;
const EPS0 = 4.0;
const KI = 0.4;
const FF = (h: number) => 2300 * Math.pow(h, 1.5);
/** Keep a tile this long after its row scrolls away, so a quick scroll back doesn't relight it. */
const TILE_LINGER_MS = 4000;
/** Forget a session's heat this long after its last cell unmounts. */
const HEAT_TTL_MS = 120_000;
/** Redraw idle coal beds this often while nothing burns (they cool in wall time). */
const COOL_REDRAW_MS = 30_000;
const SPARK_MARGIN_PX = 4;
/** The drawn tile is this many cell heights tall (aspect TW:TH), bottom on the cell. */
const TILE_SCALE = 1.2;
/** Light spill: width in tile widths, height in tile heights, peak additive intensity. */
const GLOW_W = 2.4;
const GLOW_H = 0.95;
const GLOW_MAX = 0.085;
/** Reduced motion: steps to settle a still frame, and at most this many per frame. */
const RM_SETTLE_STEPS = 90;
const RM_STEPS_PER_FRAME = 15;

// spark character per tool kind: count, T0 (K), radius (cells), launch speed (cells/s), spread (rad), life (s)
const SPARK: Record<ToolKind, { n: number; T: number; r: number; v: [number, number]; spread: number; life: number }> = {
  exec: { n: 9, T: 2350, r: 0.75, v: [15, 24], spread: 0.55, life: 1.6 },
  edit: { n: 6, T: 1650, r: 1.6, v: [10, 15], spread: 0.9, life: 2.6 },
  read: { n: 3, T: 1350, r: 0.55, v: [4, 8], spread: 0.5, life: 1.4 },
  agent: { n: 14, T: 1950, r: 1.0, v: [13, 21], spread: 1.0, life: 2.0 },
  other: { n: 2, T: 1400, r: 0.7, v: [5, 9], spread: 0.5, life: 1.2 },
};

interface Target {
  fb: WebGLFramebuffer;
  tex: WebGLTexture;
  texs: WebGLTexture[];
  w: number;
  h: number;
}
interface Pair {
  r: Target;
  w: Target;
  swap(): void;
}
interface Prog {
  p: WebGLProgram;
  loc(n: string): WebGLUniformLocation | null;
}
type Uniform = WebGLTexture | number | { i: number } | { v4: Float32Array } | number[];

interface Vis {
  puff: Float32Array;
  gust: number;
  hcmd: number;
  gain: number;
  hm: number;
  budget: number;
  coldT: number;
  frozen: boolean;
  seed: number;
  slot: number;
}

interface Cell {
  id: string;
  key: string;
  el: HTMLElement;
  occluder: HTMLElement | null;
  scroller: HTMLElement | null;
  visible: boolean;
  hiddenSince: number;
  tile: number;
  /** On screen but no tile free: the row shows its static glyph. */
  starved: boolean;
  onStarved: (starved: boolean) => void;
}

export interface HearthStats {
  gpuMs: number | null;
  frameMs: number;
  running: boolean;
  tiles: number;
  burning: number;
  frames: number;
}

function freshVis(i: number): Vis {
  return { puff: new Float32Array(SLOT_X.length), gust: 0, hcmd: 0, gain: 0, hm: 0, budget: 3, coldT: 0, frozen: true, seed: (i * 0.37 + 0.11) % 1, slot: 0 };
}

function scrollParent(el: HTMLElement): HTMLElement | null {
  let p = el.parentElement;
  while (p && p !== document.body) {
    const oy = getComputedStyle(p).overflowY;
    if (oy === "auto" || oy === "scroll") return p;
    p = p.parentElement;
  }
  return null;
}

export class HearthRenderer {
  private gl: WebGL2RenderingContext;
  private readonly nt: number;
  private readonly AW: number;
  private readonly AH: number;
  private P: Record<string, Prog> = {};
  private F: {
    vel: Pair;
    s: Pair;
    p: Pair;
    tF: Target;
    tB: Target;
    curl: Target;
    div: Target;
    part: Pair;
    meas: Target;
  };
  private lutTex: WebGLTexture;
  private pbo: WebGLBuffer;
  private vao: WebGLVertexArrayObject;
  private timerExt: { TIME_ELAPSED_EXT: number; GPU_DISJOINT_EXT: number } | null;
  private uTile: Float32Array;
  private uSrc: Float32Array;
  private uBed: Float32Array;
  private uRect: Float32Array;
  private uClip: Float32Array;
  private uGlowRect: Float32Array;
  private uGlow: Float32Array;
  private measArr: Float32Array;
  private vis: Vis[];
  private tileCell: (Cell | null)[];
  private cells = new Map<string, Cell>();
  private heats = new Map<string, { heat: SessionHeat; orphanSince: number }>();
  private simRuns: [number, number][] = [];
  private io: IntersectionObserver | null = null;
  private raf = 0;
  private last = 0;
  private acc = 0;
  private simTime = 0;
  private stillUntil = 0;
  private sparkUntil = 0;
  private settleT = 0;
  private fence: WebGLSync | null = null;
  private measT = 0;
  private q: WebGLQuery | null = null;
  private ring = 0;
  private dpr = 1;
  private box = { left: 0, top: 0, width: 0, height: 0 };
  private coolTimer = 0;
  private dead = false;
  /** Reduced motion: sim steps left before the still frame is settled.
   * A signal change resets it (never adds), and each frame spends at most
   * RM_STEPS_PER_FRAME, so a burst of card updates costs one short settle. */
  private rmSettle = RM_SETTLE_STEPS;
  private readonly one = new Float32Array(4);
  private stats: HearthStats = { gpuMs: null, frameMs: 16.7, running: false, tiles: 0, burning: 0, frames: 0 };

  static create(canvas: HTMLCanvasElement, opts: { capacity?: number; reducedMotion: boolean; onLost: () => void }): HearthRenderer | null {
    if (typeof WebGL2RenderingContext === "undefined") return null;
    let gl: WebGL2RenderingContext | null = null;
    try {
      gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, powerPreference: "low-power" });
    } catch {
      gl = null;
    }
    if (!gl || !gl.getExtension("EXT_color_buffer_float")) return null;
    try {
      return new HearthRenderer(canvas, gl, opts.capacity ?? 24, opts.reducedMotion, opts.onLost);
    } catch (e) {
      console.warn("hearth: renderer setup failed", e);
      return null;
    }
  }

  private constructor(
    private readonly canvas: HTMLCanvasElement,
    gl: WebGL2RenderingContext,
    capacity: number,
    private readonly reducedMotion: boolean,
    private readonly onLost: () => void,
  ) {
    this.gl = gl;
    this.nt = capacity;
    const S = hearthShaders(capacity);
    this.AW = S.AW;
    this.AH = S.AH;
    this.vao = gl.createVertexArray()!;
    gl.bindVertexArray(this.vao);
    const P = this.P;
    P.adv = this.prog(S.VS_FULL, S.FS_ADVECT);
    P.macc = this.prog(S.VS_FULL, S.FS_MACC);
    P.curl = this.prog(S.VS_FULL, S.FS_CURL);
    P.force = this.prog(S.VS_FULL, S.FS_FORCE);
    P.react = this.prog(S.VS_FULL, S.FS_REACT);
    P.div = this.prog(S.VS_FULL, S.FS_DIV);
    P.jac = this.prog(S.VS_FULL, S.FS_JACOBI);
    P.proj = this.prog(S.VS_FULL, S.FS_PROJECT);
    P.pup = this.prog(S.VS_FULL, S.FS_PUPDATE);
    P.pkill = this.prog(S.VS_FULL, S.FS_PKILL);
    P.comp = this.prog(S.VS_COMP, S.FS_COMP);
    P.glow = this.prog(S.VS_GLOW, S.FS_GLOW);
    P.spark = this.prog(S.VS_SPARK, S.FS_SPARK);
    P.meas = this.prog(S.VS_FULL, S.FS_MEASURE);
    const f16 = () => this.target(this.AW, this.AH, 1, gl.RGBA16F, gl.HALF_FLOAT, gl.LINEAR);
    const pair = (mk: () => Target): Pair => {
      const o = { r: mk(), w: mk(), swap() { const t = o.r; o.r = o.w; o.w = t; } };
      return o;
    };
    this.F = {
      vel: pair(f16),
      s: pair(f16),
      p: pair(f16),
      tF: f16(),
      tB: f16(),
      curl: f16(),
      div: f16(),
      part: pair(() => this.target(PW, PH, 2, gl.RGBA32F, gl.FLOAT, gl.NEAREST)),
      meas: this.target(capacity, 1, 1, gl.RGBA32F, gl.FLOAT, gl.NEAREST),
    };
    this.pbo = gl.createBuffer()!;
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, this.pbo);
    gl.bufferData(gl.PIXEL_PACK_BUFFER, capacity * 16, gl.STREAM_READ);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
    this.lutTex = this.tex(256, 1, gl.RGBA16F, gl.RGBA, gl.FLOAT, gl.LINEAR, buildBlackbodyLUT(256));
    this.timerExt = gl.getExtension("EXT_disjoint_timer_query_webgl2");
    this.uTile = new Float32Array(capacity * 4);
    this.uSrc = new Float32Array(capacity * NS * 4);
    this.uBed = new Float32Array(capacity * 4);
    this.uRect = new Float32Array(capacity * 4);
    this.uClip = new Float32Array(capacity * 4);
    this.uGlowRect = new Float32Array(capacity * 4);
    this.uGlow = new Float32Array(capacity * 4);
    this.measArr = new Float32Array(capacity * 4);
    this.vis = Array.from({ length: capacity }, (_, i) => freshVis(i));
    this.tileCell = Array.from({ length: capacity }, () => null);

    canvas.addEventListener("webglcontextlost", this.handleLost, false);
    document.addEventListener("visibilitychange", this.handleVisibility);
    window.addEventListener("scroll", this.touchSoon, { capture: true, passive: true });
    window.addEventListener("resize", this.touchSoon, { passive: true });
    if (typeof IntersectionObserver !== "undefined") {
      this.io = new IntersectionObserver(this.handleIntersect, { rootMargin: "48px 0px" });
    }
    this.coolTimer = window.setInterval(() => {
      this.rmSettle = RM_SETTLE_STEPS;
      this.requestFrame();
    }, COOL_REDRAW_MS);
  }

  // ---------------- public API ----------------

  register(id: string, key: string, el: HTMLElement, onStarved: (starved: boolean) => void = () => {}): void {
    if (this.dead) return;
    const occluder = (el.closest(".inbox-repo")?.querySelector(".inbox-repo-header") as HTMLElement | null) ?? null;
    const cell: Cell = { id, key, el, occluder, scroller: scrollParent(el), visible: !this.io, hiddenSince: 0, tile: -1, starved: false, onStarved };
    this.cells.set(id, cell);
    const entry = this.heats.get(key);
    if (entry) entry.orphanSince = 0;
    else this.heats.set(key, { heat: new SessionHeat(), orphanSince: 0 });
    this.io?.observe(el);
    this.requestFrame();
  }

  update(id: string, snap: HearthSnapshot): void {
    const cell = this.cells.get(id);
    if (!cell || this.dead) return;
    const entry = this.heats.get(cell.key);
    if (!entry) return;
    entry.heat.update(snap, performance.now() / 1000, Date.now());
    this.rmSettle = RM_SETTLE_STEPS;
    this.requestFrame();
  }

  unregister(id: string): void {
    const cell = this.cells.get(id);
    if (!cell) return;
    this.io?.unobserve(cell.el);
    this.cells.delete(id);
    if (cell.tile >= 0) this.releaseTile(cell.tile);
    if (![...this.cells.values()].some((c) => c.key === cell.key)) {
      const entry = this.heats.get(cell.key);
      if (entry) entry.orphanSince = performance.now();
    }
    this.requestFrame();
  }

  /** Rows moved (drag, reflow): redraw for a moment even if nothing burns. */
  touch(): void {
    this.stillUntil = Math.max(this.stillUntil, performance.now() + 350);
    this.requestFrame();
  }

  getStats(): HearthStats {
    return { ...this.stats };
  }

  destroy(): void {
    if (this.dead) return;
    this.dead = true;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    window.clearInterval(this.coolTimer);
    this.io?.disconnect();
    this.canvas.removeEventListener("webglcontextlost", this.handleLost, false);
    document.removeEventListener("visibilitychange", this.handleVisibility);
    window.removeEventListener("scroll", this.touchSoon, { capture: true } as EventListenerOptions);
    window.removeEventListener("resize", this.touchSoon);
    if (!this.gl.isContextLost()) this.gl.getExtension("WEBGL_lose_context")?.loseContext();
  }

  // ---------------- events ----------------

  private handleLost = (e: Event) => {
    e.preventDefault();
    this.destroy();
    this.onLost();
  };

  private handleVisibility = () => {
    if (!document.hidden) {
      this.last = 0;
      this.requestFrame();
    }
  };

  private touchSoon = () => this.touch();

  private handleIntersect = (entries: IntersectionObserverEntry[]) => {
    const now = performance.now();
    for (const entry of entries) {
      for (const cell of this.cells.values()) {
        if (cell.el !== entry.target) continue;
        if (entry.isIntersecting) cell.visible = true;
        else if (cell.visible) {
          cell.visible = false;
          cell.hiddenSince = now;
        }
      }
    }
    this.requestFrame();
  };

  private requestFrame = () => {
    if (this.dead || this.raf || document.hidden) return;
    this.raf = requestAnimationFrame(this.frame);
  };

  // ---------------- tiles ----------------

  private assignTiles(now: number) {
    for (const cell of this.cells.values()) {
      if (cell.tile >= 0 && !cell.visible && now - cell.hiddenSince > TILE_LINGER_MS) this.releaseTile(cell.tile);
    }
    for (const cell of this.cells.values()) {
      if (!cell.visible || cell.tile >= 0) continue;
      let t = this.tileCell.indexOf(null);
      if (t < 0) {
        // Steal the tile of a row that has scrolled away.
        const victim = [...this.cells.values()].find((c) => c.tile >= 0 && !c.visible);
        if (!victim) break;
        t = victim.tile;
        this.releaseTile(t);
      }
      this.tileCell[t] = cell;
      cell.tile = t;
      this.clearTile(t);
      const v = (this.vis[t] = freshVis(t));
      const heat = this.heats.get(cell.key)?.heat;
      // A row arriving on screen already burning starts part-grown, not from a spark.
      if (heat) v.hcmd = this.reducedMotion ? 0 : 0.5 * heat.target(now / 1000);
      v.frozen = false;
      this.settleT = now;
      this.rmSettle = RM_SETTLE_STEPS;
    }
    // A visible row the atlas has no room for draws its static glyph.
    for (const cell of this.cells.values()) {
      const starved = cell.visible && cell.tile < 0;
      if (starved !== cell.starved) {
        cell.starved = starved;
        cell.onStarved(starved);
      }
    }
    for (const [key, entry] of this.heats) {
      if (entry.orphanSince && now - entry.orphanSince > HEAT_TTL_MS) this.heats.delete(key);
    }
  }

  private releaseTile(t: number) {
    const cell = this.tileCell[t];
    if (cell) cell.tile = -1;
    this.tileCell[t] = null;
    this.vis[t] = freshVis(t);
    this.clearTile(t);
  }

  // ---------------- frame ----------------

  private frame = (nowMs: number) => {
    this.raf = 0;
    if (this.dead || document.hidden) return;
    const gl = this.gl;
    const t = nowMs / 1000;
    const realDt = this.last ? Math.min(0.1, t - this.last) : H;
    this.last = t;
    this.stats.frameMs += (realDt * 1000 - this.stats.frameMs) * 0.05;
    const wall = Date.now();
    this.assignTiles(nowMs);

    // Signals: advance every session; events land on the tile showing it.
    const tileOfKey = new Map<string, number>();
    for (const cell of this.cells.values()) if (cell.tile >= 0) tileOfKey.set(cell.key, cell.tile);
    for (const [key, { heat }] of this.heats) {
      const tile = tileOfKey.get(key);
      if (this.reducedMotion) heat.step(t + 3, 3, wall);
      else heat.step(t, realDt, wall, tile == null ? undefined : (e) => this.emit(tile, e, t));
    }

    const dk = Math.exp(-realDt / 0.35);
    const dg = Math.exp(-realDt / 0.6);
    const slew = realDt * 1.2; // at most 1.2 tile heights per second
    let burning = 0;
    let pendingEvents = false;
    for (let i = 0; i < this.nt; i++) {
      const cell = this.tileCell[i];
      const v = this.vis[i];
      if (!cell) continue;
      const heat = this.heats.get(cell.key)?.heat;
      const snap = heat?.snap;
      if (!heat || !snap) continue;
      if (heat.hasPending()) pendingEvents = true;
      let ht = heat.target(t);
      if (snap.mode === "waiting") {
        // Guttering: a low flame that dips and recovers, calmer than work but never still.
        const g = 0.5 + 0.5 * Math.sin(t * 1.3 + v.seed * 11) * Math.sin(t * 0.47 + v.seed * 5);
        ht = 0.2 + 0.13 * g;
      }
      // Subagents are extra flame roots while the session works.
      const roots = heat.isActive(t) && snap.mode === "working" ? Math.min(SLOT_X.length - 1, snap.subagents) : 0;
      for (let k = 1; k <= roots; k++) v.puff[k] = Math.max(v.puff[k], 0.55);
      for (let k = 0; k < v.puff.length; k++) v.puff[k] *= this.reducedMotion ? 1 : dk;
      v.gust *= dg;
      v.budget = Math.min(4, v.budget + realDt * 4);
      if (this.reducedMotion) v.hcmd = ht;
      else v.hcmd += Math.max(-slew, Math.min(slew, ht - v.hcmd));
      const busy = v.hcmd > 0.01 || v.gust > 1 || v.puff.some((x) => x > 0.05);
      if (this.reducedMotion) {
        v.frozen = !busy;
        if (busy) burning++;
      } else if (busy) {
        burning++;
        v.coldT = 0;
        v.frozen = false;
      } else if (!v.frozen && (v.coldT += realDt) > 4) {
        this.clearTile(i);
        v.frozen = true;
      }
    }
    this.stats.burning = burning;
    this.stats.tiles = this.tileCell.filter(Boolean).length;
    this.setRuns();

    const visibleTiles = this.layout();
    if (!visibleTiles) {
      // Nothing on screen: no GPU work at all. A scroll or visibility change wakes us.
      this.stats.running = false;
      if (pendingEvents && !this.reducedMotion) this.raf = requestAnimationFrame(this.frame);
      return;
    }
    this.packUniforms(wall);

    if (this.reducedMotion) {
      // A still frame: settle each lit tile from its current state a few
      // steps per frame, then draw once; nothing moves after that.
      const settling = this.rmSettle > 0 && this.simRuns.length > 0;
      if (settling) {
        const n = Math.min(RM_STEPS_PER_FRAME, this.rmSettle);
        for (let k = 0; k < n; k++) this.simStep();
        this.rmSettle -= n;
      } else this.rmSettle = 0;
      if (this.rmSettle === 0 || nowMs < this.stillUntil) this.render(false);
      this.stats.running = this.rmSettle > 0;
      if (this.rmSettle > 0) this.raf = requestAnimationFrame(this.frame);
      return;
    }

    let useQ = false;
    if (this.timerExt && !this.q) {
      this.q = gl.createQuery();
      if (this.q) {
        gl.beginQuery(this.timerExt.TIME_ELAPSED_EXT, this.q);
        useQ = true;
      }
    }
    this.acc += realDt;
    let n = 0;
    while (this.acc >= H && n < 3) {
      this.simStep();
      this.acc -= H;
      n++;
    }
    if (n === 3) this.acc = 0;
    this.render(true);
    if (this.simRuns.length) this.measureAsync(nowMs);
    if (useQ && this.timerExt) gl.endQuery(this.timerExt.TIME_ELAPSED_EXT);
    else if (this.q && this.timerExt && gl.getQueryParameter(this.q, gl.QUERY_RESULT_AVAILABLE)) {
      if (!gl.getParameter(this.timerExt.GPU_DISJOINT_EXT)) {
        const ms = gl.getQueryParameter(this.q, gl.QUERY_RESULT) / 1e6;
        this.stats.gpuMs = this.stats.gpuMs == null ? ms : this.stats.gpuMs + (ms - this.stats.gpuMs) * 0.1;
      }
      gl.deleteQuery(this.q);
      this.q = null;
    }
    this.stats.frames++;
    const keepGoing = burning > 0 || pendingEvents || t < this.sparkUntil || nowMs < this.stillUntil;
    this.stats.running = keepGoing;
    if (keepGoing) this.raf = requestAnimationFrame(this.frame);
    else if (this.q) {
      gl.deleteQuery(this.q);
      this.q = null;
    }
  };

  private emit(tile: number, e: HearthEvent, t: number) {
    const v = this.vis[tile];
    if (e.type === "prompt") {
      v.gust += 70;
      return;
    }
    if (e.type !== "tool") return;
    const slot = v.slot;
    v.slot = (v.slot + 1) % SLOT_X.length;
    if (v.budget >= 1) v.puff[slot] = Math.min(4, v.puff[slot] + (e.kind === "read" ? 1.4 : 2.4));
    this.spawnSparks(tile, e.name, slot, t);
  }

  /** Place the canvas over the strip the visible cells occupy and compute
   * each tile's rect, glow rect and clip in canvas pixels. The tile is taller
   * and wider than its cell, bottom-aligned on it, so a flame tip fades into
   * the row instead of meeting a ceiling; the glow reaches wider still.
   * Returns the visible tile count. */
  private layout(): number {
    type Box = { l: number; t: number; r: number; b: number };
    const rects: (Box | null)[] = [];
    const glows: (Box | null)[] = [];
    const clips: ({ l: number; t: number; r: number; b: number } | null)[] = [];
    let L = Infinity;
    let T = Infinity;
    let R = -Infinity;
    let B = -Infinity;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    for (let i = 0; i < this.nt; i++) {
      const cell = this.tileCell[i];
      rects[i] = null;
      clips[i] = null;
      if (!cell || !cell.visible || !cell.el.isConnected) continue;
      const r = cell.el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      let cl = 0;
      let ct = 0;
      let cr = vw;
      let cb = vh;
      if (cell.scroller) {
        const s = cell.scroller.getBoundingClientRect();
        cl = Math.max(cl, s.left);
        ct = Math.max(ct, s.top);
        cr = Math.min(cr, s.right);
        cb = Math.min(cb, s.bottom);
      }
      if (cell.occluder) {
        const o = cell.occluder.getBoundingClientRect();
        if (o.bottom > ct && o.top < r.top) ct = Math.max(ct, o.bottom);
      }
      const cx = r.left + r.width / 2;
      const th = r.height * TILE_SCALE;
      const tw = (th * TW) / TH;
      const tile = { l: cx - tw / 2, t: r.bottom - th, r: cx + tw / 2, b: r.bottom };
      const gw = tw * GLOW_W;
      const glow = { l: cx - gw / 2, t: r.bottom - th * GLOW_H, r: cx + gw / 2, b: r.bottom + r.height * 0.12 };
      const clip = { l: Math.max(cl, glow.l), t: Math.max(ct, Math.min(tile.t, glow.t) - SPARK_MARGIN_PX), r: Math.min(cr, glow.r), b: Math.min(cb, glow.b) };
      if (clip.r <= clip.l || clip.b <= clip.t) continue;
      rects[i] = tile;
      glows[i] = glow;
      clips[i] = clip;
      L = Math.min(L, clip.l);
      T = Math.min(T, clip.t);
      R = Math.max(R, clip.r);
      B = Math.max(B, clip.b);
    }
    this.uRect.fill(0);
    this.uClip.fill(0);
    this.uGlowRect.fill(0);
    if (!Number.isFinite(L)) {
      if (this.box.width) {
        this.canvas.style.display = "none";
        this.box = { left: 0, top: 0, width: 0, height: 0 };
      }
      return 0;
    }
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const box = { left: Math.floor(L), top: Math.floor(T), width: Math.ceil(R) - Math.floor(L), height: Math.ceil(B) - Math.floor(T) };
    if (box.left !== this.box.left || box.top !== this.box.top || box.width !== this.box.width || box.height !== this.box.height || dpr !== this.dpr) {
      const st = this.canvas.style;
      st.display = "block";
      st.left = `${box.left}px`;
      st.top = `${box.top}px`;
      st.width = `${box.width}px`;
      st.height = `${box.height}px`;
      const w = Math.max(1, Math.round(box.width * dpr));
      const h = Math.max(1, Math.round(box.height * dpr));
      if (this.canvas.width !== w) this.canvas.width = w;
      if (this.canvas.height !== h) this.canvas.height = h;
      this.box = box;
      this.dpr = dpr;
    }
    const bottom = box.top + box.height;
    let n = 0;
    for (let i = 0; i < this.nt; i++) {
      const r = rects[i];
      const g = glows[i];
      const c = clips[i];
      if (!r || !g || !c) continue;
      n++;
      this.uRect.set([(r.l - box.left) * dpr, (bottom - r.b) * dpr, (r.r - r.l) * dpr, (r.b - r.t) * dpr], i * 4);
      this.uGlowRect.set([(g.l - box.left) * dpr, (bottom - g.b) * dpr, (g.r - g.l) * dpr, (g.b - g.t) * dpr], i * 4);
      this.uClip.set([(c.l - box.left) * dpr, (bottom - c.b) * dpr, (c.r - box.left) * dpr, (bottom - c.t) * dpr], i * 4);
    }
    return n;
  }

  private packUniforms(wall: number) {
    for (let i = 0; i < this.nt; i++) {
      const v = this.vis[i];
      const cell = this.tileCell[i];
      const heat = cell ? this.heats.get(cell.key)?.heat : undefined;
      const snap = heat?.snap;
      let Ts = T_AMB;
      let Tc = T_AMB;
      let ash = 0;
      if (heat && snap) {
        if (snap.mode === "ended") ash = 1;
        else if (heat.isActive(performance.now() / 1000)) {
          Ts = heat.surface;
          Tc = heat.core;
        } else {
          const bed = heat.coolBed(wall);
          Ts = bed.surface;
          Tc = bed.core;
        }
      }
      const turb = 0.3 + 0.7 * Math.min(1, v.hcmd / 0.8);
      this.uTile.set([(Ts - T_AMB) / 1000, v.gust, EPS0 * turb, turb], i * 4);
      const u = v.hcmd > 0.02 ? Math.min(2500, FF(v.hcmd) * Math.exp(v.gain)) : 0;
      const b = i * NS * 4;
      this.uSrc.set([0.5, 0.08 + 0.06 * v.hcmd, u, 0.06 * u + 1.2 * v.hcmd], b);
      for (let k = 0; k < SLOT_X.length; k++) this.uSrc.set([SLOT_X[k], 0.06, v.puff[k] * 1.4, v.puff[k] * 0.6], b + (k + 1) * 4);
      this.uBed.set([Ts, Tc, v.seed, ash], i * 4);
      // Light spill follows the flame (its size, a flicker, a stoke), never idle coals.
      const lit = Math.min(1, Math.max(0, (v.hcmd - 0.04) / 0.7));
      const flick = 0.85 + 0.15 * Math.sin(this.simTime * 9.1 + v.seed * 17) * Math.sin(this.simTime * 5.3 + v.seed * 3);
      const I = GLOW_MAX * lit * flick + Math.min(0.03, v.gust * 0.0006);
      this.uGlow.set([I, I * 0.5, I * 0.16, I > 0.002 ? 1 : 0], i * 4);
    }
  }

  // ---------------- GL plumbing ----------------

  private sh(type: number, src: string): WebGLShader {
    const gl = this.gl;
    const s = gl.createShader(type)!;
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader compile failed");
    return s;
  }

  private prog(vs: string, fs: string): Prog {
    const gl = this.gl;
    const p = gl.createProgram()!;
    gl.attachShader(p, this.sh(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, this.sh(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? "program link failed");
    const cache: Record<string, WebGLUniformLocation | null> = {};
    return { p, loc: (n) => (n in cache ? cache[n] : (cache[n] = gl.getUniformLocation(p, n))) };
  }

  private tex(w: number, h: number, ifmt: number, fmt: number, type: number, filter: number, data?: Float32Array): WebGLTexture {
    const gl = this.gl;
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, ifmt, w, h, 0, fmt, type, data ?? null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  private target(w: number, h: number, n: number, ifmt: number, type: number, filter: number): Target {
    const gl = this.gl;
    const texs: WebGLTexture[] = [];
    const fb = gl.createFramebuffer()!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    for (let i = 0; i < n; i++) {
      const t = this.tex(w, h, ifmt, gl.RGBA, type, filter);
      texs.push(t);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0 + i, gl.TEXTURE_2D, t, 0);
    }
    if (n > 1) gl.drawBuffers(texs.map((_, i) => gl.COLOR_ATTACHMENT0 + i));
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("framebuffer incomplete");
    gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 0]);
    if (n > 1) gl.clearBufferfv(gl.COLOR, 1, [0, 0, 0, 0]);
    return { fb, tex: texs[0], texs, w, h };
  }

  private atlasTargets(): Target[] {
    const F = this.F;
    return [F.vel.r, F.vel.w, F.s.r, F.s.w, F.p.r, F.p.w, F.tF, F.tB, F.curl, F.div];
  }

  private clearTile(i: number) {
    const gl = this.gl;
    // Sparks carry an absolute tile index; kill this tile's before reuse.
    this.run(this.P.pkill, this.F.part.w, { u_pa: this.F.part.r.texs[0], u_pb: this.F.part.r.texs[1], u_kill: i });
    this.F.part.swap();
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(i * TW, 0, TW, this.AH);
    for (const t of this.atlasTargets()) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, t.fb);
      gl.viewport(0, 0, t.w, t.h);
      gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 0]);
    }
    gl.disable(gl.SCISSOR_TEST);
  }


  /** One solver span from the first to the last awake tile. Each pass is a
   * render pass, and on tiled GPUs a pass costs far more than the few
   * thousand extra cells a cold tile in the gap adds; a cold tile inside the
   * span only warms a little air over its coals. */
  private setRuns() {
    let lo = -1;
    let hi = -1;
    for (let i = 0; i < this.nt; i++) {
      if (this.vis[i].frozen || !this.tileCell[i]) continue;
      if (lo < 0) lo = i;
      hi = i;
    }
    this.simRuns = lo < 0 ? [] : [[lo, hi - lo + 1]];
  }

  private run(pr: Prog, tgt: Target | null, u: Record<string, Uniform>, draw?: () => void) {
    const gl = this.gl;
    gl.useProgram(pr.p);
    gl.bindFramebuffer(gl.FRAMEBUFFER, tgt ? tgt.fb : null);
    if (tgt) gl.viewport(0, 0, tgt.w, tgt.h);
    let unit = 0;
    for (const k in u) {
      const l = pr.loc(k);
      if (l === null) continue;
      const v = u[k];
      if (v instanceof WebGLTexture) {
        gl.activeTexture(gl.TEXTURE0 + unit);
        gl.bindTexture(gl.TEXTURE_2D, v);
        gl.uniform1i(l, unit++);
      } else if (typeof v === "number") gl.uniform1f(l, v);
      else if ("i" in v) gl.uniform1i(l, v.i);
      else if ("v4" in v) gl.uniform4fv(l, v.v4);
      else if (v.length === 2) gl.uniform2fv(l, v);
      else if (v.length === 4) gl.uniform4fv(l, v);
    }
    if (draw) draw();
    else if (tgt && tgt.w === this.AW && tgt.h === this.AH) {
      for (const [i0, n] of this.simRuns) {
        gl.viewport(i0 * TW, 0, n * TW, this.AH);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
    } else gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  private simStep() {
    if (!this.simRuns.length) return;
    const F = this.F;
    const P = this.P;
    this.simTime += H;
    const vr = F.vel.r.tex;
    this.run(P.adv, F.tF, { u_vel: vr, u_src: F.s.r.tex, u_dt: H, u_open: 1 });
    this.run(P.adv, F.tB, { u_vel: vr, u_src: F.tF.tex, u_dt: -H, u_open: 0 });
    this.run(P.macc, F.s.w, { u_vel: vr, u_orig: F.s.r.tex, u_fwd: F.tF.tex, u_bwd: F.tB.tex, u_dt: H });
    F.s.swap();
    this.run(P.adv, F.vel.w, { u_vel: vr, u_src: vr, u_dt: H, u_open: 0 });
    F.vel.swap();
    this.run(P.react, F.s.w, { u_s: F.s.r.tex, u_dt: H, u_time: this.simTime, u_tile: { v4: this.uTile }, u_src: { v4: this.uSrc } });
    F.s.swap();
    this.run(P.curl, F.curl, { u_vel: F.vel.r.tex });
    this.run(P.force, F.vel.w, { u_vel: F.vel.r.tex, u_curl: F.curl.tex, u_s: F.s.r.tex, u_dt: H, u_time: this.simTime, u_tile: { v4: this.uTile } });
    F.vel.swap();
    this.run(P.div, F.div, { u_vel: F.vel.r.tex });
    for (let k = 0; k < JACOBI; k++) {
      this.run(P.jac, F.p.w, { u_p: F.p.r.tex, u_div: F.div.tex });
      F.p.swap();
    }
    this.run(P.proj, F.vel.w, { u_p: F.p.r.tex, u_vel: F.vel.r.tex });
    F.vel.swap();
    if (!this.reducedMotion) {
      this.run(P.pup, F.part.w, { u_pa: F.part.r.texs[0], u_pb: F.part.r.texs[1], u_vel: F.vel.r.tex, u_dt: H });
      F.part.swap();
    }
  }

  private measureAsync(now: number) {
    const gl = this.gl;
    if (now - this.settleT < 1500) return; // let a freshly lit fire develop before the controller integrates
    if (this.fence) {
      const st = gl.clientWaitSync(this.fence, 0, 0);
      if (st === gl.TIMEOUT_EXPIRED) return;
      gl.deleteSync(this.fence);
      this.fence = null;
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, this.pbo);
      gl.getBufferSubData(gl.PIXEL_PACK_BUFFER, 0, this.measArr);
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
      const dtm = Math.min(0.25, (now - this.measT) / 1000);
      this.measT = now;
      for (let i = 0; i < this.nt; i++) {
        const v = this.vis[i];
        const hm = this.measArr[i * 4];
        v.hm += (hm - v.hm) * (1 - Math.exp(-dtm / 0.35));
        if (v.hcmd > 0.05) v.gain = Math.max(-1, Math.min(1, v.gain + KI * (v.hcmd - v.hm) * dtm));
      }
      return;
    }
    this.run(this.P.meas, this.F.meas, { u_s: this.F.s.r.tex, u_lut: this.lutTex });
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.F.meas.fb);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, this.pbo);
    gl.readPixels(0, 0, this.nt, 1, gl.RGBA, gl.FLOAT, 0);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
    this.fence = gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
  }

  private spawnSparks(i: number, name: string | null, slot: number, t: number) {
    const v = this.vis[i];
    if (this.reducedMotion || v.budget < 1) return;
    v.budget -= 1;
    const gl = this.gl;
    const sp = SPARK[kindOf(name)];
    const cx = i * TW + SLOT_X[slot] * TW;
    const one = this.one;
    for (let k = 0; k < sp.n; k++) {
      const id = this.ring;
      this.ring = (this.ring + 1) % NP;
      const px = id % PW;
      const py = (id / PW) | 0;
      const ang = (Math.random() - 0.5) * 2 * sp.spread;
      const spd = sp.v[0] + Math.random() * (sp.v[1] - sp.v[0]);
      const r = sp.r * (0.75 + 0.5 * Math.random());
      one.set([cx + (Math.random() - 0.5) * 3, 5.5 + Math.random() * 2, Math.sin(ang) * spd, Math.cos(ang) * spd]);
      gl.bindTexture(gl.TEXTURE_2D, this.F.part.r.texs[0]);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, px, py, 1, 1, gl.RGBA, gl.FLOAT, one);
      one.set([sp.T * (0.92 + 0.12 * Math.random()), sp.life * (0.7 + 0.6 * Math.random()), r, i]);
      gl.bindTexture(gl.TEXTURE_2D, this.F.part.r.texs[1]);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, px, py, 1, 1, gl.RGBA, gl.FLOAT, one);
    }
    this.sparkUntil = Math.max(this.sparkUntil, t + sp.life * 1.35);
  }

  private render(sparks: boolean) {
    const gl = this.gl;
    const F = this.F;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const cv = [this.canvas.width, this.canvas.height];
    this.run(this.P.glow, null, { u_glowRect: { v4: this.uGlowRect }, u_glow: { v4: this.uGlow }, u_clip: { v4: this.uClip }, u_canvas: cv }, () =>
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, this.nt),
    );
    this.run(
      this.P.comp,
      null,
      { u_s: F.s.r.tex, u_vel: F.vel.r.tex, u_lut: this.lutTex, u_bed: { v4: this.uBed }, u_rect: { v4: this.uRect }, u_clip: { v4: this.uClip }, u_canvas: cv, u_time: this.simTime },
      () => gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, this.nt),
    );
    if (sparks) {
      gl.blendFunc(gl.ONE, gl.ONE);
      const su = { u_pa: F.part.r.texs[0], u_pb: F.part.r.texs[1], u_lut: this.lutTex, u_rect: { v4: this.uRect }, u_clip: { v4: this.uClip }, u_canvas: cv, u_dpr: this.dpr };
      this.run(this.P.spark, null, { ...su, u_lines: { i: 1 } }, () => gl.drawArrays(gl.LINES, 0, NP * 2));
      this.run(this.P.spark, null, { ...su, u_lines: { i: 0 } }, () => gl.drawArrays(gl.POINTS, 0, NP));
    }
    gl.disable(gl.BLEND);
  }
}

export { TH, TW };
