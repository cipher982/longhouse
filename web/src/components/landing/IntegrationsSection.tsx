/**
 * Provider truth. A left-aligned capability summary that says plainly which
 * controls work for each provider.
 */

import { getLaunchProviderSupportList } from "../../lib/providers";
import { ProviderGlyph } from "../ProviderGlyph";

export function IntegrationsSection() {
  const providers = getLaunchProviderSupportList();

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">
          What works with Longhouse
        </h2>
        <p className="landing-providers-lead">
          Every provider syncs into the same searchable timeline. Launch through
          Longhouse to control supported sessions remotely.
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
      </div>
    </section>
  );
}
