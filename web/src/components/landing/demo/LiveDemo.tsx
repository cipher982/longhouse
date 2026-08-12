import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
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
 * The sandbox is warmed by `liveSession` before this ever mounts, so what the
 * visitor sees is a live bash prompt that then types its own way into Claude.
 * That startup is the demo, not an obstacle to it: watching `claude` launch is
 * the thing a Claude user recognises.
 */

type Phase = "connecting" | "starting" | "ready" | "running" | "done" | "failed";

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
  connecting: "Opening your sandbox",
  starting: "Starting Claude Code",
  ready: "Claude is ready",
  running: "Claude is working in /demo-repo",
  done: "Task complete",
  failed: "Live session unavailable.",
};

export function LiveDemo({ onFailure }: { onFailure: (reason: string) => void }) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const sessionRef = useRef<LiveSession | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const phaseRef = useRef<Phase>("connecting");
  const sentRef = useRef(false);
  const submittedRef = useRef(false);

  const [phase, setPhaseState] = useState<Phase>("connecting");
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION);
  const [elapsed, setElapsed] = useState<number | null>(null);

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  useEffect(() => {
    if (!mountRef.current || termRef.current) return;
    // Size follows the frame. This is a live PTY, not a fixed recording, so we
    // resize the sandbox to match rather than wrapping output at someone
    // else's column count.
    const term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: mountRef.current.clientWidth < 520 ? 11 : 12,
      lineHeight: 1.18,
      theme: { background: "#0b0908", foreground: "#e8e2d8" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(mountRef.current);
    termRef.current = term;

    const session = prewarmLiveSession();
    sessionRef.current = session;

    // Do not render anything until the session has a CLEAN shell. Opening the
    // panel mid-warm would otherwise show the `su` and its job-control noise
    // live, which is exactly the plumbing this flow exists to hide.
    let attached = false;
    const sink = (chunk: Uint8Array) => {
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
    };

    const attachOnce = () => {
      if (attached || session.state === "starting" || session.state === "failed") return;
      attached = true;
      session.attach(sink);
    };

    // Keep the PTY the same shape as the pane, now and on every reflow.
    const applyFit = () => {
      try {
        fit.fit();
      } catch {
        return;
      }
      sessionRef.current?.resize(term.cols, term.rows);
    };
    const observer = new ResizeObserver(() => applyFit());
    observer.observe(mountRef.current);

    const sync = () => {
      // Once the sandbox has a shell, start Claude where the visitor can see
      // it. launch() is idempotent on state, so repeated syncs are harmless.
      attachOnce();
      if (session.state === "shell") {
        applyFit();
        setPhase("starting");
        void session.launch();
      }
      if (session.state === "launching" && phaseRef.current === "connecting") {
        setPhase("starting");
      }
      if (session.state === "ready" && phaseRef.current !== "ready") {
        if (phaseRef.current === "connecting" || phaseRef.current === "starting") {
          setPhase("ready");
        }
      }
      if (session.state === "failed") {
        setPhase("failed");
        onFailure(session.failure ?? "unavailable");
      }
    };
    sync();
    const unsubscribe = session.onChange(sync);

    return () => {
      observer.disconnect();
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

  return (
    <div className="hero-live">
      <div className="hero-live-composer">
        <div className="hero-live-composer-heading">
          <div>
            <label className="hero-live-label" htmlFor="hero-live-instruction">
              Give Claude a task
            </label>
            <span className="hero-live-context">Disposable repo · Python fixture</span>
          </div>
          <span className={`hero-live-status is-${phase}`} role="status">
            <span className="hero-live-status-dot" aria-hidden="true" />
            {phase === "done" && elapsed !== null
              ? `Finished in ${elapsed.toFixed(1)}s`
              : STATUS[phase]}
          </span>
        </div>
        <textarea
          id="hero-live-instruction"
          className="hero-live-input"
          value={instruction}
          rows={2}
          disabled={phase === "running" || phase === "done"}
          onChange={(event) => setInstruction(event.target.value)}
        />
        <div className="hero-live-controls">
          <span className="hero-live-hint">Edit the prompt, then send it to the live agent.</span>
          <button
            type="button"
            className="hero-live-run"
            onClick={run}
            disabled={phase !== "ready"}
          >
            <span>{phase === "running" ? "Running…" : "Run task"}</span>
            <span className="hero-live-run-arrow" aria-hidden="true">→</span>
          </button>
        </div>
      </div>
      <div className="hero-live-terminal-window">
        <div className="hero-live-terminal-chrome">
          <span className="hero-live-terminal-dots" aria-hidden="true">
            <i /><i /><i />
          </span>
          <span className="hero-live-terminal-title">demo@cloudchamber · /demo-repo</span>
          <span className="hero-live-terminal-meta">
            <span aria-hidden="true" /> ephemeral
          </span>
        </div>
        <div className="hero-live-terminal" ref={mountRef} />
      </div>
    </div>
  );
}
