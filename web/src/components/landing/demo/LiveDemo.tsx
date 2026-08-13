import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { PhoneFrame } from "./PhoneFrame";
import { PhoneSessionScreen, type PhoneRuntimeTone } from "./PhoneSessionScreen";
import { prewarmLiveSession, type LiveSession } from "./liveSession";
import { buildLiveTimelineModel, flattenLiveItems } from "./liveProjection";
import type { TimelineItem } from "../../../lib/sessionWorkspace";

type Phase = "connecting" | "starting" | "ready" | "running" | "done" | "failed";

export const DEFAULT_INSTRUCTION =
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

function terminalText(term: Terminal): string {
  const buffer = term.buffer.active;
  const start = Math.max(0, buffer.baseY);
  const lines: string[] = [];
  for (let index = start; index < buffer.length; index += 1) {
    lines.push(buffer.getLine(index)?.translateToString(true) ?? "");
  }
  return lines.join("\n");
}

function phoneState(active: boolean, phase: Phase): {
  label: string;
  detail?: string;
  tone: PhoneRuntimeTone;
} {
  if (!active) return { label: "Live demo", detail: "Tap to connect", tone: "waiting" };
  if (phase === "connecting") return { label: "Connecting", detail: "Opening sandbox", tone: "starting" };
  if (phase === "starting") return { label: "Starting", detail: "Claude Code", tone: "starting" };
  if (phase === "ready") return { label: "Ready", detail: "Waiting for input", tone: "ready" };
  if (phase === "running") return { label: "Working", detail: "On demo-repo", tone: "working" };
  if (phase === "done") return { label: "Complete", detail: "Task finished", tone: "done" };
  return { label: "Unavailable", detail: "Try again later", tone: "failed" };
}

export function LiveDemo({ active }: { active: boolean }) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const sessionRef = useRef<LiveSession | null>(null);
  const phaseRef = useRef<Phase>("connecting");
  const sentRef = useRef(false);
  const submittedRef = useRef(false);
  const submittedInstructionRef = useRef("");
  const sawWorkingRef = useRef(false);

  const [phase, setPhaseState] = useState<Phase>("connecting");
  const [draft, setDraft] = useState(DEFAULT_INSTRUCTION);
  const [submittedInstruction, setSubmittedInstruction] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [items, setItems] = useState<TimelineItem[]>([]);

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const markDone = useCallback(() => {
    if (phaseRef.current !== "running") return;
    setPhase("done");
  }, [setPhase]);

  useEffect(() => {
    if (!active || !mountRef.current || termRef.current) return;

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
    let attached = false;

    const inspectScreen = () => {
      if (!submittedRef.current || phaseRef.current !== "running") return;
      const screen = signalOf(terminalText(term));
      if (screen.includes("esctointerrupt")) sawWorkingRef.current = true;
      if (sawWorkingRef.current && screen.includes("foragents") && !screen.includes("esctointerrupt")) {
        markDone();
      }
    };

    const sink = (chunk: Uint8Array) => {
      term.write(chunk, inspectScreen);
      const signal = signalOf(session.transcript);
      if (sentRef.current && !submittedRef.current) {
        const typed = signalOf(submittedInstructionRef.current);
        if (typed && signal.includes(typed)) {
          submittedRef.current = true;
          session.send("\r");
        }
      }
    };

    const attachOnce = () => {
      if (attached || session.state === "starting" || session.state === "failed") return;
      attached = true;
      session.attach(sink);
    };

    const applyFit = () => {
      try {
        fit.fit();
      } catch {
        return;
      }
      sessionRef.current?.resize(term.cols, term.rows);
    };
    const observer = new ResizeObserver(applyFit);
    observer.observe(mountRef.current);

    const sync = () => {
      attachOnce();
      if (session.state === "shell") {
        applyFit();
        setPhase("starting");
        void session.launch();
      }
      if (session.state === "launching" && phaseRef.current === "connecting") {
        setPhase("starting");
      }
      if (
        session.state === "ready" &&
        (phaseRef.current === "connecting" || phaseRef.current === "starting")
      ) {
        setPhase("ready");
      }
      if (session.state === "failed") setPhase("failed");
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
  }, [active, markDone, setPhase]);

  useEffect(() => {
    if (!sent || phase === "failed") return;
    let cancelled = false;
    let polling = false;

    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const next = await sessionRef.current?.events();
        if (!cancelled && next) {
          setItems(flattenLiveItems(buildLiveTimelineModel(next).items));
        }
      } catch {
        // The terminal remains the source of completion truth. A transient
        // transcript read simply retries on the next poll.
      } finally {
        polling = false;
      }
    };

    void poll();
    if (phase === "running") {
      const interval = window.setInterval(() => void poll(), 350);
      return () => {
        cancelled = true;
        window.clearInterval(interval);
      };
    }

    const finalPoll = window.setTimeout(() => void poll(), 250);
    return () => {
      cancelled = true;
      window.clearTimeout(finalPoll);
    };
  }, [phase, sent]);

  const run = useCallback(() => {
    const session = sessionRef.current;
    if (!session || phaseRef.current !== "ready" || sentRef.current) return;
    const instruction = draft.trim();
    if (!instruction) return;
    sentRef.current = true;
    submittedInstructionRef.current = instruction;
    setSubmittedInstruction(instruction);
    setDraft("");
    setSent(true);
    setPhase("running");
    session.send(instruction);
  }, [draft, setPhase]);

  const runtime = phoneState(active, phase);

  return (
    <>
      <PhoneFrame>
        <PhoneSessionScreen
          title="Live demo repo"
          transcript={{
            sentMessage: submittedInstruction ?? undefined,
            items,
          }}
          composerText={draft}
          composerDisabled={sent || phase === "failed"}
          runtimeLabel={runtime.label}
          runtimeDetail={runtime.detail}
          runtimeTone={runtime.tone}
          sendEnabled={active && phase === "ready"}
          sent={sent}
          working={phase === "running"}
          onComposerChange={setDraft}
          onSend={run}
        />
      </PhoneFrame>

      <div className="hero-live-terminal-window steer-live-terminal">
        <div className="hero-live-terminal-chrome">
          <span className="hero-live-terminal-dots" aria-hidden="true">
            <i /><i /><i />
          </span>
          <span className="hero-live-terminal-title">demo@cloudchamber · /demo-repo</span>
          <span className="hero-live-terminal-meta">
            <span aria-hidden="true" /> ephemeral
          </span>
        </div>
        <div className="hero-live-screen">
          <div className="hero-live-terminal" ref={mountRef} />
          {(!active || phase === "connecting") && (
            <div className="hero-live-cover">
              {active && <span className="hero-live-spinner" aria-hidden="true" />}
              <span>
                {active
                  ? "Opening a disposable Linux sandbox…"
                  : "Tap the phone to start a live Claude Code session."}
              </span>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
