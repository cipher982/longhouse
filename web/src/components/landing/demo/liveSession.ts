import { eventsUrl, sessionUrl, terminalUrl } from "./liveDemoConfig";
import type { AgentSessionProjectionResponse } from "../../../services/api/agents";

/**
 * A warm sandbox that exists before anyone looks at it.
 *
 * The split matters: creating the sandbox is boring plumbing and happens
 * invisibly on the first sign of a real human, but LAUNCHING Claude is the
 * part worth watching, so it waits for the UI and then types itself out on
 * screen. Hiding that behind a spinner turned the wait into dead time; showing
 * it turns the wait into the demo.
 */

/**
 * The tuning flags live in a shell alias set up during warm-up, the same way a
 * real user's dotfiles would carry them, so what the visitor types stays short
 * and still runs exactly what is shown. Nothing here is a prettified
 * stand-in — bash expands this into the full invocation, and a self-referential
 * alias does not recurse.
 */
const CLAUDE_ALIAS =
  `alias claude='claude --bare --effort low ` +
  `--append-system-prompt "tiny throwaway workspace, minimal change, run tests once, stop, <=3 sentence responses"'`;

/** What the visitor watches being typed. */
export const CLAUDE_COMMAND = "claude --dangerously-skip-permissions";

export type SessionState = "starting" | "shell" | "launching" | "ready" | "failed";

function signalOf(raw: string): string {
  return (
    raw
      // ESC is the point below: these match real terminal escape sequences
      // coming off the PTY, so the control character is intentional.
      // eslint-disable-next-line no-control-regex
      .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
      // eslint-disable-next-line no-control-regex
      .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
      .replace(/\s+/g, "")
      .toLowerCase()
  );
}

export class LiveSession {
  state: SessionState = "starting";
  failure: string | null = null;
  transcript = "";

  private socket: WebSocket | null = null;
  private buffer: Uint8Array[] = [];
  private sink: ((chunk: Uint8Array) => void) | null = null;
  private watchers = new Set<() => void>();
  private decoder = new TextDecoder();
  private cols = 0;
  private rows = 0;
  private sessionId: string | null = null;
  private closedByClient = false;

  onChange(watcher: () => void): () => void {
    this.watchers.add(watcher);
    return () => this.watchers.delete(watcher);
  }

  private notify() {
    for (const watcher of this.watchers) watcher();
  }

  private fail(reason: string) {
    if (this.state === "failed") return;
    this.state = "failed";
    this.failure = reason;
    this.notify();
  }

  async start(): Promise<void> {
    let sessionId: string;
    try {
      const response = await fetch(sessionUrl(), { method: "POST" });
      if (response.status === 429) throw new Error("busy");
      if (!response.ok) throw new Error(`session ${response.status}`);
      sessionId = (await response.json()).sessionId;
      this.sessionId = sessionId;
    } catch (error) {
      this.fail(error instanceof Error ? error.message : "session failed");
      return;
    }

    const socket = new WebSocket(terminalUrl(sessionId));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    this.closedByClient = false;

    socket.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) {
        try {
          const message = JSON.parse(event.data as string);
          if (message.type === "ready" && this.state === "starting") {
            void this.openCleanShell();
          }
          if (message.type === "error") this.fail(message.message ?? "pty error");
        } catch {
          /* terminal chunks can begin with a brace */
        }
        return;
      }
      const chunk = new Uint8Array(event.data);
      this.transcript = (
        this.transcript + this.decoder.decode(chunk, { stream: true })
      ).slice(-60000);

      if (this.sink) this.sink(chunk);
      else this.buffer.push(chunk);

      if (this.state === "launching" && signalOf(this.transcript).includes("bypasspermissionson")) {
        this.state = "ready";
        this.notify();
      }
    };

    socket.onerror = () => this.fail("connection error");
    socket.onclose = () => {
      if (!this.closedByClient) this.fail("connection closed");
    };
  }

  /**
   * Hand the accumulated startup to a terminal in one write, then stream.
   * Everything before this point is invisible by design.
   */
  attach(sink: (chunk: Uint8Array) => void): void {
    for (const chunk of this.buffer) sink(chunk);
    this.buffer = [];
    this.sink = sink;
  }

  detach(): void {
    this.sink = null;
  }

  /**
   * Drop to the demo user and clear the screen, all before anyone is looking.
   * `su` into a PTY without job control prints "cannot set terminal process
   * group" noise, which is plumbing nobody should read — so it happens during
   * warm-up and the buffer is discarded, leaving a clean demo@ prompt as the
   * first thing the visitor ever sees.
   */
  private async openCleanShell(): Promise<void> {
    this.send("su -p demo -s /bin/bash\r");
    await new Promise((resolve) => setTimeout(resolve, 700));
    this.send(`${CLAUDE_ALIAS}\r`);
    await new Promise((resolve) => setTimeout(resolve, 250));
    this.send("cd /demo-repo && clear\r");
    // Wait out the echo AND the clear before discarding, or a stray fragment
    // of the word "clear" survives the wipe and shows up glued to the prompt.
    await new Promise((resolve) => setTimeout(resolve, 900));
    this.buffer = [];
    this.transcript = "";
    this.state = "shell";
    this.notify();
  }

  /** Type a line the way a person does, so the visitor can read it. */
  async type(text: string, msPerChar = 28): Promise<void> {
    for (const char of text) {
      this.send(char);
      await new Promise((resolve) => setTimeout(resolve, msPerChar));
    }
    this.send("\r");
  }

  /** Start Claude on screen — this is the part worth watching. */
  async launch(): Promise<void> {
    if (this.state !== "shell") return;
    this.state = "launching";
    this.notify();
    await this.type(CLAUDE_COMMAND);
  }

  /**
   * Resize the real PTY to match the rendered terminal. Without this the
   * sandbox keeps its initial 64x20 and the output wraps at 64 columns
   * regardless of how wide the frame is, which both looks wrong and pushes the
   * launch command off the top of the viewport.
   */
  resize(cols: number, rows: number): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    if (cols === this.cols && rows === this.rows) return;
    this.cols = cols;
    this.rows = rows;
    this.socket.send(JSON.stringify({ type: "resize", cols, rows }));

    // A resize reflows text that was already emitted at the old width, which
    // leaves overlapping glyphs ("issionsce, stop,"). Repaint rather than live
    // with the artifact: an idle shell can just clear, and Claude redraws its
    // whole UI on Ctrl+L.
    if (this.state === "shell") this.send("clear\r");
    else if (this.state === "ready") this.send("\x0c");
  }

  send(text: string): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(new TextEncoder().encode(text));
    }
  }

  async events(): Promise<AgentSessionProjectionResponse> {
    if (!this.sessionId) {
      return {
        root_session_id: "",
        focus_session_id: "",
        head_session_id: "",
        path_session_ids: [],
        items: [],
        total: 0,
        branch_mode: "head",
      };
    }
    const response = await fetch(eventsUrl(this.sessionId), { cache: "no-store" });
    if (!response.ok) throw new Error(`events ${response.status}`);
    return (await response.json()) as AgentSessionProjectionResponse;
  }

  close(): void {
    this.closedByClient = true;
    this.socket?.close();
    this.socket = null;
  }
}

let current: LiveSession | null = null;

/**
 * Begin warming. Safe to call repeatedly; only the first call starts a
 * sandbox. Callers should only invoke this on evidence of a real human,
 * never on bare page load, or every crawler costs us a container.
 */
export function prewarmLiveSession(): LiveSession {
  if (!current || current.state === "failed") {
    current = new LiveSession();
    void current.start();
  }
  return current;
}

export function releaseLiveSession(): void {
  current?.close();
  current = null;
}
