import { memo } from "react";
import {
  AGENT_TILES,
  PROVIDERS,
  claudeTile,
  codexTile,
} from "@longhouse/video/demo";
import { ResponsiveTerminal } from "./ResponsiveTerminal";
import { ramp } from "./ease";

/** Beat 1: real provider CLIs, really running — recorded PTY replays. */

const RECORDINGS = { claudeTile, codexTile } as const;

function Beat({ tLocal }: { tLocal: number }) {
  return (
    <div className="hero-demo-agents">
      {AGENT_TILES.map((tile, i) => {
        const provider = PROVIDERS.find((p) => p.id === tile.providerId);
        if (!provider) return null;
        const enter = ramp(tLocal, 0.15 + i * 0.3, 0.5);
        const tSec = Math.min(
          tile.window.startSec + Math.max(0, tLocal - 0.4),
          tile.window.endSec,
        );
        return (
          <div
            key={provider.id}
            className="hero-demo-agents-tile"
            style={{
              opacity: enter,
              transform: `translateY(${((1 - enter) * 20).toFixed(2)}px)`,
            }}
          >
            <ResponsiveTerminal
              timeline={RECORDINGS[tile.recording]}
              tSec={tSec}
              title={provider.name}
              accent={provider.color}
              detail={provider.machine}
            />
          </div>
        );
      })}
    </div>
  );
}

export const AgentsBeat = memo(Beat);
