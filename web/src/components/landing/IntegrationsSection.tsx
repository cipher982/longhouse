/**
 * Provider truth. Every claim derives from the generated provider contract;
 * this component only turns those booleans into readable landing-page copy.
 */

import { getLaunchProviderSupportList, type LaunchProviderSupport } from "../../lib/providers";
import { ProviderGlyph } from "../ProviderGlyph";

type Capability = {
  key: "search" | "launch" | "interrupt" | "steer" | "resume";
  label: string;
  supported: (provider: LaunchProviderSupport) => boolean;
};

const CAPABILITIES: Capability[] = [
  { key: "search", label: "Search", supported: () => true },
  { key: "launch", label: "Launch", supported: (provider) => provider.launchAndSend },
  { key: "interrupt", label: "Interrupt", supported: (provider) => provider.interrupt },
  { key: "steer", label: "Mid-turn", supported: (provider) => provider.steerMidTurn },
  { key: "resume", label: "Resume", supported: (provider) => provider.resume },
];

function providerSummary(provider: LaunchProviderSupport): string {
  if (provider.steerMidTurn) {
    return "Full remote control, including steering during a turn.";
  }
  if (provider.launchAndSend && provider.resume) {
    return "Launch, interrupt, and resume. Your next instruction lands when the current turn ends.";
  }
  if (provider.launchAndSend) {
    return "Launch, send, and interrupt. Resume is not available yet.";
  }
  return "Syncs in for watching and search. No Longhouse control path.";
}

function CapabilityChip({ capability, provider }: { capability: Capability; provider: LaunchProviderSupport }) {
  const supported = capability.supported(provider);
  return (
    <span
      className={`landing-provider-capability ${supported ? "is-supported" : "is-unsupported"}`}
      data-capability={capability.key}
      data-supported={supported ? "true" : "false"}
      aria-label={`${capability.label}: ${supported ? "supported" : "not supported"}`}
    >
      {capability.label}
    </span>
  );
}

export function IntegrationsSection() {
  const providers = getLaunchProviderSupportList();

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">Control support, provider by provider.</h2>
        <p className="landing-providers-lead">
          Every CLI below syncs into one searchable timeline. What changes is how far
          control goes once you launch through Longhouse.
        </p>

        <div className="landing-providers-universal">
          <p>
            <strong>Sync, timeline, and full-text search</strong>
            <span>Included for every provider.</span>
          </p>
          <div className="landing-providers-universal-list" aria-label="Providers with timeline and search support">
            {providers.map((provider) => (
              <span className="landing-providers-universal-item" key={provider.id}>
                <ProviderGlyph provider={provider.id} size={16} variant="bare" />
                {provider.marketingName}
              </span>
            ))}
          </div>
        </div>

        <ul className="landing-provider-rails">
          {providers.map((provider) => (
            <li className="landing-provider-rail" data-provider={provider.id} key={provider.id}>
              <div className="landing-providers-provider-label">
                <span className="landing-provider-row-glyph">
                  <ProviderGlyph provider={provider.id} size={16} variant="bare" />
                </span>
                <strong className="landing-provider-row-name">{provider.marketingName}</strong>
              </div>
              <p className="landing-provider-summary">{providerSummary(provider)}</p>
              <div className="landing-provider-capabilities" aria-label={`${provider.marketingName} capabilities`}>
                {CAPABILITIES.map((capability) => (
                  <CapabilityChip capability={capability} provider={provider} key={capability.key} />
                ))}
              </div>
            </li>
          ))}
        </ul>

        <p className="landing-providers-source">Generated from the provider contract, not hand-maintained.</p>
      </div>
    </section>
  );
}
