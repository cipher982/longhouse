import { sessionUrl, terminalUrl } from "./liveDemoConfig";

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
 * What the visitor watches being typed. This is the real invocation, not a
 * prettified stand-in: display copy that drifts from what actually ran is a
 * mistake this repo has already paid for once. The leash that used to live in
 * --append-system-prompt moved into the workspace's CLAUDE.md so the command
 * stays something a person would plausibly type.
 */
export const CLAUDE_COMMAND =
  "claude --bare --effort low " +
  '--append-system-prompt "tiny throwaway workspace, minimal change, run tests once, stop, <=3 sentence responses" ' +
  "--dangerously-skip-permissions";

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
    } catch (error) {
      this.fail(error instanceof Error ? error.message : "session failed");
      return;
    }

    const socket = new WebSocket(terminalUrl(sessionId));
    socket.binaryType = "arraybuffer";
    this.socket = socket;

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
      // 1006 before the composer appears is the known sandbox transient.
      if (this.state !== "ready") this.fail("connection closed");
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
    this.send("cd /demo-repo && clear\r");
    await new Promise((resolve) => setTimeout(resolve, 500));
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

  send(text: string): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(new TextEncoder().encode(text));
    }
  }

  close(): void {
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
