/**
 * Provider truth. Every chip and every sentence derives from two layers: the
 * shipped proof graph says a live test exists for a claim ("covered"), and the
 * Runtime Host's certification says that test currently passes against the
 * real binary ("certified"). A claim is lit only when certified. This
 * component only turns those states into copy.
 */

import type { ProvenChips } from "../../generated/provider-capabilities";
import {
  certifiedChips,
  chipCertification,
  useProviderCertification,
  type CertificationState,
  type ChipKey,
  type ProviderCertificationPayload,
} from "../../lib/providerCertification";
import { getLaunchProviderSupportList, type LaunchProviderSupport } from "../../lib/providers";
import { ProviderGlyph } from "../ProviderGlyph";

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

const STATE_DESCRIPTION: Record<CertificationState, string> = {
  certified: "proven against the real binary",
  unverified: "tested, awaiting a current proof",
  stale: "proof expired",
  failing: "latest proof failing",
  unproven: "not yet proven",
  unavailable: "certification status unavailable",
};

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
export function providerSummary(certified: ProvenChips, unavailable = false): string {
  if (unavailable) {
    return "Certification status is unavailable right now.";
  }
  const claims: Array<[boolean, string]> = [
    [certified.launchAndSend, "launch and send"],
    [certified.interrupt, "interrupt"],
    [certified.steerMidTurn, "mid-turn steering"],
    [certified.resume, "resume"],
  ];
  const have = claims.filter(([proven]) => proven).map(([, label]) => label);
  const missing = claims.filter(([proven]) => !proven).map(([, label]) => label);
  if (missing.length === 0) {
    return "Full remote control, including steering during a turn.";
  }
  const pending = `${capitalize(joinClause(missing))} ${missing.length === 1 ? "is" : "are"} not currently proven.`;
  if (have.length === 0) return pending;
  return `${capitalize(joinClause(have))}. ${pending}`;
}

function capitalize(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function CapabilityChip({
  capability,
  provider,
  certification,
}: {
  capability: Capability;
  provider: LaunchProviderSupport;
  certification: ProviderCertificationPayload | null;
}) {
  const state = chipCertification(provider.id, capability.chip, provider.proven, certification);
  const supported = state === "certified";
  const pending = state !== "certified" && state !== "unproven";
  return (
    <span
      className={`landing-provider-capability ${supported ? "is-supported" : "is-unsupported"}${pending ? " is-pending" : ""}`}
      data-capability={capability.key}
      data-supported={supported ? "true" : "false"}
      data-certification={state}
      title={`${capability.label}: ${STATE_DESCRIPTION[state]}`}
      aria-label={`${capability.label}: ${STATE_DESCRIPTION[state]}`}
    >
      {capability.label}
    </span>
  );
}

export function IntegrationsSection() {
  const providers = getLaunchProviderSupportList();
  const certification = useProviderCertification();
  const unavailable = certification === null;
  const certified = new Map(providers.map((provider) => [provider.id, certifiedChips(provider.id, provider.proven, certification)]));
  const searchable = providers.filter((provider) => certified.get(provider.id)?.search);

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">Control support, provider by provider.</h2>
        <p className="landing-providers-lead">
          What Longhouse can do with each CLI once you launch through it. A capability
          lights up only when a current live test proves it against the real binary.
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
              <p className="landing-provider-summary">{providerSummary(certified.get(provider.id)!, unavailable)}</p>
              <div className="landing-provider-capabilities" aria-label={`${provider.marketingName} capabilities`}>
                {CAPABILITIES.map((capability) => (
                  <CapabilityChip capability={capability} provider={provider} certification={certification} key={capability.key} />
                ))}
              </div>
            </li>
          ))}
        </ul>

        <p className="landing-providers-source">
          Lit only while a current live test passes against the real binary.
          {unavailable ? " Certification status could not be loaded right now; chips below reflect the shipped proof graph only." : ""}
        </p>
      </div>
    </section>
  );
}
