/**
 * Provider truth. Distinct shape: a left-aligned honest "what works on each
 * provider" table-ish strip, not a centered punchline + card grid. The honesty
 * IS the design here — say plainly where each control path stands today.
 */

import { getLaunchProviderSupportList } from "../../lib/providers";
import { ProviderGlyph } from "../ProviderGlyph";

export function IntegrationsSection() {
  const providers = getLaunchProviderSupportList();

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">
          What works on each provider, honestly.
        </h2>
        <p className="landing-providers-lead">
          Every provider lands in the same timeline and search. Live control depends
          on how mature each path is today — so here&rsquo;s the plain truth.
        </p>

        <ul className="landing-providers-rows">
          {providers.map((p) => (
            <li key={p.id} className="landing-provider-row">
              <span className="landing-provider-row-glyph">
                <ProviderGlyph provider={p.id} size={34} />
              </span>
              <span className="landing-provider-row-name">{p.marketingName}</span>
              <span className="landing-provider-row-desc">{p.cardDescription}</span>
              <span className="landing-provider-row-status">{p.statusLabel}</span>
            </li>
          ))}
        </ul>

        <p className="landing-providers-foot">
          Codex launch-through-Longhouse is supported; OpenCode supports managed
          send, interrupt, launch, and terminate without active-turn steer;
          Antigravity is managed observe-only today. Claude is still the
          strongest continuation path. Older Google CLI JSON imports remain searchable under Antigravity.
        </p>
      </div>
    </section>
  );
}
