/**
 * Provider support. Every chip and sentence derives from the generated
 * provider contract: a chip is lit when the contract says Longhouse implements
 * that capability for the provider. This component only turns those facts into
 * copy.
 */

import { getLaunchProviderSupportList, type LaunchProviderSupport } from "@/shared/lib/providers";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";

type ChipKey = "search" | "launchAndSend" | "interrupt" | "steerMidTurn" | "resume";
type Claims = Pick<LaunchProviderSupport, ChipKey>;

type Capability = {
  key: "search" | "launch" | "interrupt" | "steer" | "resume";
  label: string;
  chip: ChipKey;
};

const CAPABILITIES: Capability[] = [
  { key: "search", label: "Search", chip: "search" },
  { key: "launch", label: "Launch", chip: "launchAndSend" },
  { key: "interrupt", label: "Interrupt", chip: "interrupt" },
  { key: "steer", label: "Mid-turn", chip: "steerMidTurn" },
  { key: "resume", label: "Resume", chip: "resume" },
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
export function providerSummary(claims: Claims): string {
  const rows: Array<[boolean, string]> = [
    [claims.launchAndSend, "launch and send"],
    [claims.interrupt, "interrupt"],
    [claims.steerMidTurn, "mid-turn steering"],
    [claims.resume, "resume"],
  ];
  const have = rows.filter(([supported]) => supported).map(([, label]) => label);
  const missing = rows.filter(([supported]) => !supported).map(([, label]) => label);
  if (missing.length === 0) {
    return "Full remote control, including steering during a turn.";
  }
  const unsupported = `${capitalize(joinClause(missing))} ${missing.length === 1 ? "is" : "are"} not supported.`;
  if (have.length === 0) return unsupported;
  return `${capitalize(joinClause(have))}. ${unsupported}`;
}

function capitalize(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function CapabilityChip({ capability, provider }: { capability: Capability; provider: LaunchProviderSupport }) {
  const supported = provider[capability.chip];
  const description = supported ? "supported" : "not supported";
  return (
    <span
      className={`landing-provider-capability ${supported ? "is-supported" : "is-unsupported"}`}
      data-capability={capability.key}
      data-supported={supported ? "true" : "false"}
      title={`${capability.label}: ${description}`}
      aria-label={`${capability.label}: ${description}`}
    >
      {capability.label}
    </span>
  );
}

export function IntegrationsSection() {
  const providers = getLaunchProviderSupportList();
  const searchable = providers.filter((provider) => provider.search);

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">Control support, provider by provider.</h2>
        <p className="landing-providers-lead">What Longhouse can do with each CLI once you launch through it.</p>

        {searchable.length > 0 ? (
          <div className="landing-providers-universal">
            <p>
              <strong>Sync, timeline, and full-text search</strong>
              <span>{searchable.length === providers.length ? "Included for every provider." : "Available for these providers."}</span>
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
      </div>
    </section>
  );
}
