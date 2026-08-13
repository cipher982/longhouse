import { memo } from "react";
import { PROVIDERS } from "@longhouse/video/demo";
import type { DemoStory } from "../../../lib/demoSimulation";
import { ramp } from "./ease";

/** Beat 2: every session, one system — the Longhouse panel. */

const RECENCY = ["2m ago", "8m ago", "31m ago"];

function Beat({ tLocal, story }: { tLocal: number; story: DemoStory | null }) {
  const panelIn = ramp(tLocal, 0.05, 0.45);
  return (
    <div
      className="hero-demo-panel"
      style={{
        opacity: panelIn,
        transform: `scale(${(0.96 + panelIn * 0.04).toFixed(4)})`,
      }}
    >
      <div className="hero-demo-panel-header">
        <span className="hero-demo-panel-brand">Longhouse</span>
        <span className="hero-demo-panel-sub">every agent · every machine</span>
        <span className="hero-demo-panel-count">4 sessions</span>
      </div>
      {PROVIDERS.map((provider, i) => {
        const rowIn = ramp(tLocal, 0.35 + i * 0.28, 0.45);
        const live = i === 0;
        return (
          <div
            key={provider.id}
            className="hero-demo-panel-row"
            style={{
              opacity: rowIn,
              transform: `translateX(${((1 - rowIn) * 22).toFixed(2)}px)`,
            }}
          >
            <span
              className="hero-demo-panel-dot"
              style={{ background: provider.color }}
            />
            <span className="hero-demo-panel-task">
              {i === 0 && story ? story.shortLabel : provider.task}
            </span>
            <span className="hero-demo-panel-provider">{provider.name}</span>
            <span className="hero-demo-panel-machine">{provider.machine}</span>
            <span
              className={`hero-demo-panel-status${live ? " is-live" : ""}`}
            >
              {live ? "live · steerable" : RECENCY[i - 1]}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export const UnifyBeat = memo(Beat);
