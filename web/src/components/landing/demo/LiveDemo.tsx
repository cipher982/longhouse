import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { LIVE_COLS, LIVE_ROWS } from "./liveDemoConfig";
import { prewarmLiveSession, type LiveSession } from "./liveSession";

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
 * The sandbox is warmed by `liveSession` before this ever mounts, and the
 * terminal stays covered until Claude has reached its composer. Nobody should
 * watch our `su ... exec claude` line or the boot render — the pane is
 * revealed already idle at a prompt, which is the state a Claude user
 * recognises.
 */

type Phase = "warming" | "ready" | "running" | "done" | "failed";

const DEFAULT_INSTRUCTION =
  "Fix the off-by-one bug in count_items in inventory.py, then run: python3 test_inventory.py";

function signalOf(raw: string): string {
  return (
    raw
      // eslint-disable-next-line no-control-regex
      .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
      // eslint-disable-next-line no-control-regex
      .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
      .replace(/\s+/g, "")
      .toLowerCase()
  );
}

const STATUS: Record<Phase, string> = {
  warming: "Waking a disposable sandbox…",
  ready: "Claude Code is ready. Edit the instruction, then run it.",
  running: "Claude Code is working in /demo-repo",
  done: "Finished.",
  failed: "Live session unavailable.",
};

export function LiveDemo({ onFailure }: { onFailure: (reason: string) => void }) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const sessionRef = useRef<LiveSession | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const phaseRef = useRef<Phase>("warming");
  const sentRef = useRef(false);
  const submittedRef = useRef(false);

  const [phase, setPhaseState] = useState<Phase>("warming");
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION);
  const [elapsed, setElapsed] = useState<number | null>(null);

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  useEffect(() => {
    if (!mountRef.current || termRef.current) return;
    // Geometry is fixed to the sandbox PTY: a mismatch corrupts alt-screen TUIs.
    const term = new Terminal({
      cols: LIVE_COLS,
      rows: LIVE_ROWS,
      convertEol: true,
      cursorBlink: true,
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: 12,
      theme: { background: "#0b0908", foreground: "#e8e2d8" },
    });
    term.loadAddon(new FitAddon());
    term.open(mountRef.current);
    termRef.current = term;

    // Attach to the already-warming session and replay its buffered startup in
    // one go, so the pane resolves straight to the composer.
    const session = prewarmLiveSession();
    sessionRef.current = session;
    session.attach((chunk) => {
      term.write(chunk);
      const signal = signalOf(session.transcript);
      if (sentRef.current && !submittedRef.current) {
        const typed = signalOf(instructionRef.current);
        if (typed && signal.includes(typed)) {
          submittedRef.current = true;
          session.send("\r");
        }
      }
      if (submittedRef.current && phaseRef.current === "running" && signal.includes("alltestspassed")) {
        setPhase("done");
        if (startedAtRef.current) {
          setElapsed((performance.now() - startedAtRef.current) / 1000);
        }
      }
    });

    const sync = () => {
      if (session.state === "ready" && phaseRef.current === "warming") {
        // Claude renders inline rather than on an alt-screen, so the shell's
        // echoed launch line sits above its UI in scrollback. Wipe the pane and
        // ask Claude to repaint (Ctrl+L) so the reveal is a clean idle prompt.
        term.reset();
        session.send("\x0c");
        setPhase("ready");
      }
      if (session.state === "failed") {
        setPhase("failed");
        onFailure(session.failure ?? "unavailable");
      }
    };
    sync();
    const unsubscribe = session.onChange(sync);

    return () => {
      unsubscribe();
      session.detach();
      term.dispose();
      termRef.current = null;
    };
  }, [onFailure, setPhase]);

  // The attach callback closes over the first render, so read the live value.
  const instructionRef = useRef(instruction);
  useEffect(() => {
    instructionRef.current = instruction;
  }, [instruction]);

  const run = useCallback(() => {
    const session = sessionRef.current;
    if (!session || phaseRef.current !== "ready" || sentRef.current) return;
    sentRef.current = true;
    startedAtRef.current = performance.now();
    setPhase("running");
    session.send(instruction);
  }, [instruction, setPhase]);

  const covered = phase === "warming";

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
      />
      <div className="hero-live-controls">
        <button
          type="button"
          className="hero-live-run"
          onClick={run}
          disabled={phase !== "ready"}
        >
          {phase === "running" ? "Running…" : "Run it"}
        </button>
        <span className="hero-live-status" role="status">
          {phase === "done" && elapsed !== null
            ? `Finished in ${elapsed.toFixed(1)}s`
            : STATUS[phase]}
        </span>
      </div>
      <div className="hero-live-screen">
        <div className="hero-live-terminal" ref={mountRef} />
        {covered && (
          <div className="hero-live-cover">
            <span className="hero-live-spinner" aria-hidden="true" />
            <span>Waking a disposable sandbox…</span>
          </div>
        )}
      </div>
    </div>
  );
}
