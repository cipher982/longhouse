/**
 * Hearth reel: four mock sessions on the shipped fire (HearthProvider +
 * HearthLamp), zoomed so the fires fill the frame, each with the status
 * label that tells you why it burns. Dev-only page: /hearth-reel.html.
 *
 * Live, it loops scene.ts forever. scripts/record-hearth-reel.mjs drives it
 * on a virtual clock (?record) and calls window.__hearthReel.restart() so
 * the recording starts from a known scene time.
 */

import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { HearthLamp, HearthProvider } from "@/shared/instruments/hearth/Hearth";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { DURATION, STATIONS, WARMUP, stationAt } from "./scene";
import "@/app/styles/tokens.css";
import "@/shared/instruments/instruments.css";
import "./App.css";

const RECORD = new URLSearchParams(window.location.search).has("record");

declare global {
  interface Window {
    __hearthReel?: { warmup: number; duration: number; restart(): void; sceneTime(): number };
  }
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

  return (
    <HearthProvider>
      <main className="hearth-reel">
        {STATIONS.map((station) => {
          const v = stationAt(station, t, wallAt);
          return (
            <section className="hearth-reel__station" key={`${v.key}-${gen}`}>
              <HearthLamp sessionKey={`${v.key}-${gen}`} snapshot={v.snapshot} state={v.state} label={v.label} />
              <div className="hearth-reel__title">
                <ProviderGlyph provider={v.provider} size={20} />
                <span>{v.title}</span>
              </div>
              <div className="hearth-reel__detail">{v.detail || "\u00a0"}</div>
            </section>
          );
        })}
      </main>
      <div className="hearth-reel__mark">Longhouse</div>
    </HearthProvider>
  );
}

createRoot(document.getElementById("hearth-reel-root")!).render(<Reel />);
