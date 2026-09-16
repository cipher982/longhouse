/**
 * Provider truth. Every chip and every sentence derives from the generated
 * proof edges: a claim is lit only when the provider factory runs a live-token
 * assertion for it. This component only turns those booleans into copy.
 */

import { getLaunchProviderSupportList, type LaunchProviderSupport } from "../../lib/providers";
import { ProviderGlyph } from "../ProviderGlyph";

type Capability = {
  key: "search" | "launch" | "interrupt" | "steer" | "resume";
  label: string;
  supported: (provider: LaunchProviderSupport) => boolean;
};

const CAPABILITIES: Capability[] = [
  { key: "search", label: "Search", supported: (provider) => provider.proven.search },
  { key: "launch", label: "Launch", supported: (provider) => provider.proven.launchAndSend },
  { key: "interrupt", label: "Interrupt", supported: (provider) => provider.proven.interrupt },
  { key: "steer", label: "Mid-turn", supported: (provider) => provider.proven.steerMidTurn },
  { key: "resume", label: "Resume", supported: (provider) => provider.proven.resume },
];

function joinClause(parts: string[]): string {
  if (parts.length < 3) return parts.join(" and ");
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}

/**
 * Built from the booleans rather than matched against them. The branch version
 * of this never read `provider.interrupt`, so Antigravity's row claimed
 * "Launch, send, and interrupt" directly beside a chip reading Interrupt: not
 * supported.
 */
function providerSummary(provider: LaunchProviderSupport): string {
  const claims: Array<[boolean, string]> = [
    [provider.proven.launchAndSend, "launch and send"],
    [provider.proven.interrupt, "interrupt"],
    [provider.proven.steerMidTurn, "mid-turn steering"],
    [provider.proven.resume, "resume"],
  ];
  const have = claims.filter(([proven]) => proven).map(([, label]) => label);
  const missing = claims.filter(([proven]) => !proven).map(([, label]) => label);
  if (missing.length === 0) {
    return "Full remote control, including steering during a turn.";
  }
  const pending = `${capitalize(joinClause(missing))} ${missing.length === 1 ? "is" : "are"} not yet release-proven.`;
  if (have.length === 0) return pending;
  return `${capitalize(joinClause(have))}. ${pending}`;
}

function capitalize(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
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
  const searchable = providers.filter((provider) => provider.proven.search);

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">Control support, provider by provider.</h2>
        <p className="landing-providers-lead">
          What Longhouse can do with each CLI once you launch through it. A capability
          lights up only when the provider factory proves it against the real binary.
        </p>

        {searchable.length > 0 ? (
          <div className="landing-providers-universal">
            <p>
              <strong>Sync, timeline, and full-text search</strong>
              <span>
                {searchable.length === providers.length ? "Included for every provider." : "Release-proven for these providers."}
              </span>
            </p>
            <div className="landing-providers-universal-list" aria-label="Providers with timeline and search support">
              {searchable.map((provider) => (
                <span className="landing-providers-universal-item" key={provider.id}>
                  <ProviderGlyph provider={provider.id} size={16} variant="bare" />
                  {provider.marketingName}
                </span>
              ))}
            </div>
          </div>
        ) : null}

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

        <p className="landing-providers-source">
          Lit only where the provider factory runs a live test against the real binary.
        </p>
      </div>
    </section>
  );
}
