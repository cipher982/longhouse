import { memo } from "react";
import {
  AGENT_TILES,
  PROVIDERS,
  claudeTile,
  codexTile,
  opencodeTile,
} from "@longhouse/video/demo";
import { ResponsiveTerminal } from "./ResponsiveTerminal";

/** Beat 1: real provider CLIs, really running — recorded PTY replays. */

const RECORDINGS = { claudeTile, codexTile, opencodeTile } as const;

function Beat({ tLocal }: { tLocal: number }) {
  return (
    <div className="hero-demo-agents">
      {AGENT_TILES.map((tile) => {
        const provider = PROVIDERS.find((p) => p.id === tile.providerId);
        if (!provider) return null;
        const tSec = Math.min(
          tile.window.startSec + Math.max(0, tLocal - 0.4),
          tile.window.endSec,
        );
        return (
          <div
            key={provider.id}
            className="hero-demo-agents-tile"
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
