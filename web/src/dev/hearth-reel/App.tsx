/**
 * Hearth reel: the real timeline (TimelineInbox, with its shelf, History,
 * rows and the shipped fire) over scripted mock sessions, inside a static
 * copy of the app header and timeline toolbar. Dev-only page:
 * /hearth-reel.html.
 *
 * Live, it loops scene.ts forever. scripts/record-hearth-reel.mjs drives it
 * on a virtual clock (?record) and calls window.__hearthReel.restart() so
 * the recording starts from a known scene time.
 */

import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TimelineInbox } from "@/features/timeline/TimelineInbox";
import { Sparkline } from "@/shared/instruments/Sparkline";
import { Button, Input, PageShell } from "@/shared/ui";
import { SwarmLogo } from "@/shared/ui/SwarmLogo";
import { DURATION, WARMUP, cardsAt } from "./scene";
import "@/app/styles/app.css";
import "@/app/styles/layout.css";
import "@/features/timeline/sessions.css";
import "@/features/timeline/inbox.css";
import "./App.css";

const RECORD = new URLSearchParams(window.location.search).has("record");
const queryClient = new QueryClient();
const SPARK = [0, 1, 0, 0, 2, 1, 0, 3, 2, 4, 3, 6];

declare global {
  interface Window {
    __hearthReel?: { warmup: number; duration: number; restart(): void; sceneTime(): number };
  }
}

function Header() {
  return (
    <header className="main-header">
      <div className="header-left">
        <div className="header-brand">
          <a href="/timeline" className="brand-link" onClick={(e) => e.preventDefault()}>
            <SwarmLogo size={24} className="brand-logo" />
            <h1>Longhouse</h1>
          </a>
        </div>
      </div>
      <nav className="header-nav" aria-label="Main navigation">
        {["Timeline", "Machines", "Health"].map((label, i) => (
          <button key={label} type="button" className={`nav-tab${i === 0 ? " nav-tab--active" : ""}`}>
            <span className="nav-tab-label">{label}</span>
            {i === 0 ? <span className="nav-tab-indicator" aria-hidden="true" /> : null}
          </button>
        ))}
      </nav>
      <div className="header-actions">
        <span className="nav-status">
          <span className="nav-status-dot" aria-hidden="true" />
          API healthy, 3 of 3 machines up
        </span>
      </div>
    </header>
  );
}

function Toolbar({ count }: { count: number }) {
  return (
    <div className="sessions-toolbar">
      <div className="sessions-search-row">
        <Input type="search" placeholder="Search sessions" className="sessions-search-input" readOnly />
        <button type="button" className="sessions-ai-toggle">
          <span className="sessions-ai-toggle-label">AI</span>
        </button>
      </div>
      <div className="sessions-toolbar-actions">
        <div className="sessions-header-actions">
          <span className="sessions-header-count">{count} tasks</span>
          <Button variant="primary" size="sm">
            Start a session
          </Button>
        </div>
        <span className="instrument-sparkline-label">
          <Sparkline data={SPARK} width={120} height={20} live />
          activity, last hour
        </span>
      </div>
    </div>
  );
}

function Reel() {
  const clock = useRef({ start: performance.now(), wall0: Date.now() });
  const [gen, setGen] = useState(0);
  const [t, setT] = useState(-WARMUP);

  useEffect(() => {
    const restart = () => {
      clock.current = { start: performance.now(), wall0: Date.now() };
      setGen((g) => g + 1);
      setT(-WARMUP);
    };
    const sceneTime = () => (performance.now() - clock.current.start) / 1000 - WARMUP;
    window.__hearthReel = { warmup: WARMUP, duration: DURATION, restart, sceneTime };
    let raf = 0;
    const loop = () => {
      const now = sceneTime();
      if (!RECORD && now > DURATION + 1.5) restart();
      // Cards change on beats, not every frame; 20 Hz is finer than any beat.
      else setT(Math.floor(now * 20) / 20);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      delete window.__hearthReel;
    };
  }, []);

  const { wall0 } = clock.current;
  const wallAt = (s: number) => wall0 + (s + WARMUP) * 1000;
  const cards = cardsAt(t, wallAt, gen);

  return (
    <>
      <Header />
      <div id="app-container">
        <PageShell size="wide" className="sessions-page-container">
          <div className="sessions-page">
            <Toolbar count={cards.length} />
            <TimelineInbox key={gen} sessions={cards} onSessionClick={() => {}} relativeNowMs={wallAt(t)} />
          </div>
        </PageShell>
      </div>
    </>
  );
}

createRoot(document.getElementById("hearth-reel-root")!).render(
  <QueryClientProvider client={queryClient}>
    <Reel />
  </QueryClientProvider>,
);
