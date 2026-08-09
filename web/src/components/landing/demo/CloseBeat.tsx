import { memo } from "react";
import { PROVIDERS } from "@longhouse/video/demo";
import { ramp } from "./ease";

/** Beat 4: wordmark close (the footer caption carries the tagline). */

function Beat({ tLocal }: { tLocal: number }) {
  const markIn = ramp(tLocal, 0.1, 0.5);
  const namesIn = ramp(tLocal, 0.7, 0.5);
  return (
    <div className="hero-demo-close">
      <span
        className="hero-demo-close-wordmark"
        style={{ opacity: markIn, transform: `translateY(${((1 - markIn) * 12).toFixed(2)}px)` }}
      >
        Longhouse
      </span>
      <span className="hero-demo-close-providers" style={{ opacity: namesIn }}>
        {PROVIDERS.map((p) => (
          <span key={p.id} style={{ color: p.color }}>
            {p.name}
          </span>
        ))}
      </span>
    </div>
  );
}

export const CloseBeat = memo(Beat);
