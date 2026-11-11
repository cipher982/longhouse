/**
 * SPIKE (terminal-truth-baseline, track C): draws a provider's real terminal,
 * mirrored by the Runtime Host, in an xterm.js widget.
 *
 * Query: sid (session id), host (host:port of the Runtime Host, default this
 * origin), mode = fit | native | phone, measure=0 to skip latency markers.
 * Wire format: see engine/src/terminal_mirror.rs.
 *
 * Latency markers: the stand-in TUI writes `[M<6 digits> <16 digit epoch us>]`
 * into its output. For each one this page records when the frame carrying it
 * arrived, when xterm finished parsing it, when it first appeared in the DOM
 * and when the next task ran after that (the closest a page can get to "the
 * frame after the DOM changed was painted"; it is not a GPU present time).
 */
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import "./style.css";

const KIND_OUTPUT = 1;
const KIND_SIZE = 2;
const KIND_SNAPSHOT = 3;
const KIND_GAP = 4;
const KIND_INPUT = 16;
const KIND_RESIZE = 17;
const HEADER = 21;

type MarkerTiming = {
  seq: number;
  emitUs: number; // stand-in's write time
  readUs: number; // relay read time (frame stamp)
  recvUs: number; // frame arrived in the browser
  parsedUs: number; // xterm write callback
  domUs: number | null; // first MutationObserver pass that finds it in the DOM
  paintUs: number | null; // first task after that pass
  viaSnapshot: boolean;
};

const params = new URLSearchParams(location.search);
const sid = params.get("sid") ?? "none";
const host = params.get("host") ?? location.host;
const mode = params.get("mode") ?? "native";
const measure = params.get("measure") !== "0";
const fontSize = Number(params.get("font") ?? (mode === "phone" ? 12 : 14));

const nowUs = () => Math.round((performance.timeOrigin + performance.now()) * 1000);

const root = document.getElementById("mirror-root")!;
root.className = `mirror mode-${mode}`;
root.innerHTML = `
  <div class="mirror-bar"><span id="mirror-state">connecting</span><span id="mirror-size"></span></div>
  <div class="mirror-viewport"><div class="mirror-scale"><div id="mirror-term"></div></div></div>`;
const stateEl = document.getElementById("mirror-state")!;
const sizeEl = document.getElementById("mirror-size")!;
const viewport = root.querySelector<HTMLElement>(".mirror-viewport")!;
const scaler = root.querySelector<HTMLElement>(".mirror-scale")!;

const term = new Terminal({
  cols: 80,
  rows: 24,
  scrollback: 0,
  fontSize,
  fontFamily: "Menlo, 'DejaVu Sans Mono', monospace",
  allowProposedApi: true,
  cursorBlink: false,
  theme: { background: "#111417", foreground: "#d8dde0" },
});
term.open(document.getElementById("mirror-term")!);

function layout() {
  const screen = term.element?.querySelector<HTMLElement>(".xterm-screen");
  if (!screen) return;
  const width = screen.offsetWidth;
  const height = screen.offsetHeight;
  sizeEl.textContent = ` ${term.cols}x${term.rows}`;
  if (mode === "fit") {
    const scale = Math.min(1, viewport.clientWidth / Math.max(width, 1));
    scaler.style.transform = `scale(${scale})`;
    scaler.style.width = `${width}px`;
    scaler.style.height = `${height}px`;
    viewport.style.height = `${Math.ceil(height * scale)}px`;
  } else {
    scaler.style.transform = "";
    scaler.style.width = `${width}px`;
    scaler.style.height = `${height}px`;
    viewport.style.height = "";
  }
}

const state = {
  frames: 0,
  bytes: 0,
  snapshots: 0,
  gaps: 0,
  resizes: 0,
  producer: false,
  open: false,
  lastOffset: 0,
};
const markers: MarkerTiming[] = [];
const pending = new Map<number, MarkerTiming>();
const decoder = new TextDecoder();
let tail = "";
let sawSnapshotYet = false;

function setState(text: string) {
  stateEl.textContent = text;
}

// Everything that touches the terminal goes through term.write so a resize or
// reset is applied after the bytes before it have been parsed.
function sequenced(action: () => void) {
  term.write("", action);
}

function onMarkers(text: string, readUs: number, recvUs: number, viaSnapshot: boolean) {
  if (!measure) return;
  const combined = tail + text;
  const found: Array<[number, number]> = [];
  for (const match of combined.matchAll(/\[M(\d{6}) (\d{16})\]/g)) {
    found.push([Number(match[1]), Number(match[2])]);
  }
  tail = combined.slice(-40);
  // A marker split across two frames is found once the second one arrives, and
  // the tail would find it again as complete on the next frame; skip repeats.
  const fresh = found.filter(([seq]) => !pending.has(seq) && !markers.some((m) => m.seq === seq));
  if (!fresh.length) return;
  const entries = fresh.map(([seq, emitUs]) => {
    const entry: MarkerTiming = {
      seq,
      emitUs,
      readUs,
      recvUs,
      parsedUs: 0,
      domUs: null,
      paintUs: null,
      viaSnapshot,
    };
    pending.set(seq, entry);
    return entry;
  });
  term.write("", () => {
    const parsed = nowUs();
    for (const entry of entries) entry.parsedUs = parsed;
    observe();
  });
}

function domText(): string {
  return term.element?.querySelector(".xterm-rows")?.textContent ?? "";
}

let observing = false;
function observe() {
  if (observing || pending.size === 0) return;
  observing = true;
  // Check after xterm's own render frame has run, then again on every DOM
  // mutation until each pending marker appears or ages out.
  const check = () => {
    const text = domText();
    const at = nowUs();
    for (const [seq, entry] of pending) {
      if (entry.domUs === null && text.includes(`[M${String(seq).padStart(6, "0")} `)) {
        entry.domUs = at;
        setTimeout(() => {
          entry.paintUs = nowUs();
          pending.delete(seq);
          markers.push(entry);
        }, 0);
      } else if (entry.domUs === null && at - entry.parsedUs > 500_000) {
        // Overwritten before a frame drew it (two markers inside one frame).
        pending.delete(seq);
        markers.push(entry);
      }
    }
    if (pending.size === 0) {
      observer.disconnect();
      observing = false;
    }
  };
  const observer = new MutationObserver(check);
  observer.observe(term.element!, { subtree: true, childList: true, characterData: true });
  requestAnimationFrame(() => setTimeout(check, 0));
  // Aged-out markers need a timer, because nothing may mutate the DOM again.
  const sweep = setInterval(() => {
    if (!observing) return clearInterval(sweep);
    check();
  }, 100);
}

// sid=none draws no stream: the page is only a terminal to write recorded bytes into.
const socket = sid === "none" ? new WebSocket("ws://127.0.0.1:1/unused") : new WebSocket(`ws://${host}/api/terminal-mirror/${sid}/view`);
socket.binaryType = "arraybuffer";
socket.onopen = () => {
  state.open = true;
  setState("waiting for snapshot");
};
socket.onclose = () => {
  state.open = false;
  setState("closed");
};
socket.onmessage = (event) => {
  const recvUs = nowUs();
  if (typeof event.data === "string") {
    const status = JSON.parse(event.data) as { type: string; producer: boolean };
    state.producer = status.producer;
    setState(status.producer ? (sawSnapshotYet ? "live" : "waiting for snapshot") : "no producer");
    return;
  }
  const data = new Uint8Array(event.data as ArrayBuffer);
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const kind = data[0];
  const tsUs = Number(view.getBigUint64(1, true));
  const offset = Number(view.getBigUint64(9, true));
  const cols = view.getUint16(17, true);
  const rows = view.getUint16(19, true);
  const payload = data.subarray(HEADER);
  state.frames += 1;
  state.bytes += payload.length;
  if (kind === KIND_OUTPUT) {
    state.lastOffset = offset;
    term.write(payload);
    onMarkers(decoder.decode(payload, { stream: true }), tsUs, recvUs, false);
  } else if (kind === KIND_SIZE) {
    state.resizes += 1;
    sequenced(() => {
      term.resize(cols, rows);
      layout();
    });
  } else if (kind === KIND_SNAPSHOT) {
    state.snapshots += 1;
    state.lastOffset = offset;
    sawSnapshotYet = true;
    tail = "";
    sequenced(() => {
      term.reset();
      term.resize(cols, rows);
      layout();
    });
    term.write(payload);
    onMarkers(decoder.decode(payload), tsUs, recvUs, true);
    setState("live");
    if (mode === "phone" && !phoneResizeSent) {
      phoneResizeSent = true;
      setTimeout(requestPhoneSize, 300); // after the snapshot's resize has been laid out
    }
  } else if (kind === KIND_GAP) {
    state.gaps += 1;
    setState("stale (frames dropped)");
  }
};

let phoneResizeSent = false;
function requestPhoneSize() {
  // The PTY is the only size the provider draws at, so the phone asks for one
  // that fits it. This changes the size for every other viewer, the owner's
  // terminal included.
  const cell = term.element!.querySelector<HTMLElement>(".xterm-screen")!.offsetWidth / term.cols;
  const lineHeight = term.element!.querySelector<HTMLElement>(".xterm-screen")!.offsetHeight / term.rows;
  if (!cell || !lineHeight) return;
  const cols = Math.max(20, Math.floor((window.innerWidth - 8) / cell));
  const rows = Math.max(10, Math.floor((window.innerHeight - 40) / lineHeight));
  const message = new Uint8Array(5);
  new DataView(message.buffer).setUint16(1, cols, true);
  new DataView(message.buffer).setUint16(3, rows, true);
  message[0] = KIND_RESIZE;
  socket.send(message);
}

const encoder = new TextEncoder();
term.onData((text) => {
  const bytes = encoder.encode(text);
  const message = new Uint8Array(bytes.length + 1);
  message[0] = KIND_INPUT;
  message.set(bytes, 1);
  if (socket.readyState === WebSocket.OPEN) socket.send(message);
});

function screenRows(): string[] {
  const buffer = term.buffer.active;
  const lines: string[] = [];
  for (let y = 0; y < term.rows; y += 1) {
    lines.push(buffer.getLine(buffer.viewportY + y)?.translateToString(true) ?? "");
  }
  return lines;
}

(window as unknown as { __mirror: unknown }).__mirror = {
  term,
  state,
  markers,
  screenRows,
  alternate: () => term.buffer.active.type === "alternate",
  // Writes raw bytes, for drawing a recorded stream (the owner's terminal).
  writeRaw: (bytes: number[], cols?: number, rows?: number) => {
    if (cols && rows) term.resize(cols, rows);
    term.write(new Uint8Array(bytes), layout);
  },
  // Same, from base64, so a long recorded stream crosses the test bridge as one string.
  writeB64: (b64: string, cols?: number, rows?: number) => {
    const raw = atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    if (cols && rows) term.resize(cols, rows);
    term.write(bytes, layout);
  },
  requestSize: (cols: number, rows: number) => {
    const message = new Uint8Array(5);
    new DataView(message.buffer).setUint16(1, cols, true);
    new DataView(message.buffer).setUint16(3, rows, true);
    message[0] = KIND_RESIZE;
    socket.send(message);
  },
  resize: (cols: number, rows: number) => {
    term.resize(cols, rows);
    layout();
  },
  layout,
  sendInput: (text: string) => {
    const bytes = encoder.encode(text);
    const message = new Uint8Array(bytes.length + 1);
    message[0] = KIND_INPUT;
    message.set(bytes, 1);
    socket.send(message);
  },
  idle: () => new Promise<void>((resolve) => term.write("", () => requestAnimationFrame(() => resolve()))),
};
layout();
window.addEventListener("resize", layout);
