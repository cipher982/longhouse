import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import {
  LIVE_COLS,
  LIVE_ROWS,
  sessionUrl,
  terminalUrl,
} from "./liveDemoConfig";

/**
 * The LIVE half of the hero demo: real Claude Code executing a visitor's own
 * instruction in a disposable Cloudflare sandbox, streamed over a PTY
 * WebSocket into xterm.
 *
 * This is deliberately NOT a beat. `HeroDemo` gives every child a `tLocal`
 * from a looping rAF clock and crossfades them; a live session has no
 * timeline, no seek and no loop. It shares the hero's visual slot, not its
 * clock.
 *
 * Startup is hidden behind the visitor reading and typing: the sandbox is
 * created and Claude launched to its composer as soon as this component
 * mounts, so "Run it" lands on an already-warm session. Measured 3.1s
 * click-to-done that way, against ~12s cold.
 *
 * Mounting is itself the gate. This component is only rendered after an
 * explicit "Type your own" click, so warming on mount cannot spin up a sandbox
 * for a crawler — nothing warms from merely rendering the landing page.
 */

type Phase = "idle" | "warming" | "ready" | "running" | "done" | "failed";

const LAUNCH_CLAUDE =
  "su -p demo -s /bin/bash -c 'exec /usr/local/bin/claude --effort low --bare " +
  '--append-system-prompt "tiny throwaway workspace, minimal change, run tests once, stop, <=3 sentence responses" ' +
  "--dangerously-skip-permissions'\r";

const DEFAULT_INSTRUCTION =
  "Fix the off-by-one bug in count_items in inventory.py, then run: python3 test_inventory.py";

/**
 * Strip ANSI/OSC before matching. The PTY interleaves escape sequences with
 * the text — a TUI reflow can emit "bypass\x1b[3Gpermissions" — so matching
 * raw bytes silently never fires. Whitespace is stripped for the same reason:
 * cursor moves land mid-phrase, turning "all tests passed" into
 * "all testspassed".
 */
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

const STATUS: Record<Phase, string> = {
  idle: "Type an instruction, then run it.",
  warming: "Starting a disposable sandbox…",
  ready: "Sandbox ready — press Run when you are.",
  running: "Claude Code is working in /demo-repo",
  done: "Finished.",
  failed: "Live session unavailable.",
};

export function LiveDemo({
  token,
  onFailure,
}: {
  token: string;
  onFailure: (reason: string) => void;
}) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const transcriptRef = useRef("");
  const startedAtRef = useRef<number | null>(null);
  const phaseRef = useRef<Phase>("idle");
  const sentRef = useRef(false);
  const submittedRef = useRef(false);
  const runRequestedRef = useRef(false);
  const composerReadyRef = useRef(false);
  const autoWarmedRef = useRef(false);

  const [phase, setPhaseState] = useState<Phase>("idle");
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION);
  const [elapsed, setElapsed] = useState<number | null>(null);

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const fail = useCallback(
    (reason: string) => {
      if (phaseRef.current === "failed" || phaseRef.current === "done") return;
      setPhase("failed");
      onFailure(reason);
    },
    [onFailure, setPhase],
  );

  // Mount xterm once. Geometry is fixed to the sandbox PTY: a mismatch
  // corrupts alt-screen TUIs.
  useEffect(() => {
    if (!mountRef.current || termRef.current) return;
    const term = new Terminal({
      cols: LIVE_COLS,
      rows: LIVE_ROWS,
      convertEol: true,
      cursorBlink: true,
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: 12,
      theme: { background: "#0b0908", foreground: "#e8e2d8" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(mountRef.current);
    termRef.current = term;
    return () => {
      term.dispose();
      termRef.current = null;
    };
  }, []);

  // Always drop the sandbox when this component goes away, so navigating off
  // or switching back to the recorded demo does not strand a live session.
  useEffect(
    () => () => {
      socketRef.current?.close();
      socketRef.current = null;
    },
    [],
  );

  const send = useCallback((text: string) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(new TextEncoder().encode(text));
    }
  }, []);

  const maybeSendInstruction = useCallback(() => {
    if (sentRef.current || !composerReadyRef.current || !runRequestedRef.current) return;
    sentRef.current = true;
    startedAtRef.current = performance.now();
    setPhase("running");
    send(instruction);
  }, [instruction, send, setPhase]);

  const warm = useCallback(async () => {
    if (phaseRef.current !== "idle") return;
    setPhase("warming");
    let sessionId: string;
    try {
      const response = await fetch(sessionUrl(token), { method: "POST" });
      if (!response.ok) throw new Error(`session ${response.status}`);
      sessionId = (await response.json()).sessionId;
    } catch (error) {
      fail(error instanceof Error ? error.message : "session failed");
      return;
    }

    const socket = new WebSocket(terminalUrl(sessionId, token));
    socket.binaryType = "arraybuffer";
    socketRef.current = socket;

    socket.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) {
        try {
          const message = JSON.parse(event.data as string);
          if (message.type === "ready") send(LAUNCH_CLAUDE);
          if (message.type === "error") fail(message.message ?? "pty error");
        } catch {
          /* terminal chunks can start with a brace */
        }
        return;
      }
      const bytes = new Uint8Array(event.data);
      termRef.current?.write(bytes);
      transcriptRef.current = (
        transcriptRef.current + new TextDecoder().decode(bytes)
      ).slice(-40000);
      const signal = signalOf(transcriptRef.current);

      if (!composerReadyRef.current && signal.includes("bypasspermissionson")) {
        composerReadyRef.current = true;
        if (phaseRef.current === "warming") setPhase("ready");
        maybeSendInstruction();
      }
      if (sentRef.current && !submittedRef.current) {
        const typed = signalOf(instruction);
        if (typed && signal.includes(typed)) {
          submittedRef.current = true;
          send("\r");
        }
      }
      // Matched whitespace-stripped: the TUI reflows and emits cursor moves
      // mid-phrase, so "all tests passed" can arrive as "all testspassed".
      if (submittedRef.current && signal.includes("alltestspassed")) {
        if (phaseRef.current !== "done") {
          setPhase("done");
          if (startedAtRef.current) {
            setElapsed((performance.now() - startedAtRef.current) / 1000);
          }
        }
      }
    };

    socket.onerror = () => fail("connection error");
    socket.onclose = () => {
      // 1006 mid-run is the known Cloudflare sandbox transient.
      if (phaseRef.current === "warming" || phaseRef.current === "running") {
        fail("connection closed");
      }
    };
  }, [fail, instruction, maybeSendInstruction, send, setPhase, token]);

  // Warm as soon as the live demo is opened. This component only mounts after
  // an explicit "Type your own" click, which is stronger intent than the
  // textarea focus we would otherwise wait for — and no crawler clicks it. It
  // also means the terminal shows real boot output immediately instead of
  // sitting as an empty black rectangle while the visitor reads.
  useEffect(() => {
    if (autoWarmedRef.current) return;
    autoWarmedRef.current = true;
    void warm();
  }, [warm]);

  const run = useCallback(() => {
    runRequestedRef.current = true;
    if (phaseRef.current === "idle") void warm();
    maybeSendInstruction();
  }, [maybeSendInstruction, warm]);

  const busy = phase === "running" || phase === "warming";

  return (
    <div className="hero-live">
      <label className="hero-live-label" htmlFor="hero-live-instruction">
        Your instruction
      </label>
      <textarea
        id="hero-live-instruction"
        className="hero-live-input"
        value={instruction}
        rows={2}
        disabled={phase === "running" || phase === "done"}
        onChange={(event) => setInstruction(event.target.value)}
        onFocus={() => void warm()}
      />
      <div className="hero-live-controls">
        <button
          type="button"
          className="hero-live-run"
          onClick={run}
          disabled={busy || phase === "done"}
        >
          {phase === "running" ? "Running…" : "Run it"}
        </button>
        <span className="hero-live-status" role="status">
          {phase === "done" && elapsed !== null
            ? `Finished in ${elapsed.toFixed(1)}s`
            : STATUS[phase]}
        </span>
      </div>
      <div className="hero-live-terminal" ref={mountRef} />
    </div>
  );
}
