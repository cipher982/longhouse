/**
 * The certified landing layer.
 *
 * Two layers, never merged (control-plane docs/specs/provider-chip-proof-graph.md):
 * `proven` in the generated capabilities says a live factory test exists for
 * every edge behind a chip ("covered"); this module asks the Runtime Host which
 * of those tests have a current admissible pass ("certified"). A chip lights
 * only when both agree. Anything we cannot confirm -- no response, an older
 * server, an unknown state -- renders as unverified, never as lit.
 */

import { useEffect, useState } from "react";
import type { GeneratedProviderId, ProvenChips } from "../generated/provider-capabilities";

export type ChipKey = keyof ProvenChips;
export type CertificationState = "certified" | "unverified" | "stale" | "failing" | "unproven" | "unavailable";

export type ChipRequirement = {
  declared_in: string;
  scenario_id: string;
  assertion_id: string;
  variant: string | null;
  proof_status: string;
  latest_outcome: string | null;
  proven_at: string | null;
  longhouse_git_sha: string | null;
  provider_version: string | null;
};

export type ProviderCertificationPayload = {
  artifact_kind: "provider_chip_certification";
  generated_at: string;
  providers: Array<{
    provider: string;
    chips: Partial<Record<ChipKey, { state: CertificationState; requirements: ChipRequirement[] }>>;
  }>;
};

export const CERTIFICATION_URL = "/api/public/provider-certification";
const KNOWN_STATES = new Set<CertificationState>(["certified", "unverified", "stale", "failing", "unproven"]);

let pending: Promise<ProviderCertificationPayload | null> | null = null;

export function fetchProviderCertification(): Promise<ProviderCertificationPayload | null> {
  pending ??= fetch(CERTIFICATION_URL, { headers: { Accept: "application/json" } })
    .then(async (response) => {
      if (!response.ok) return null;
      const body = (await response.json()) as ProviderCertificationPayload;
      return body?.artifact_kind === "provider_chip_certification" && Array.isArray(body.providers) ? body : null;
    })
    .catch(() => null);
  return pending;
}

/** Test seam: forget the memoized request. */
export function resetProviderCertificationCache(): void {
  pending = null;
}

export function chipCertification(
  provider: GeneratedProviderId,
  chip: ChipKey,
  covered: ProvenChips,
  payload: ProviderCertificationPayload | null,
): CertificationState {
  if (!covered[chip]) return "unproven";
  // No payload means the certification layer could not be read at all -- a
  // network, origin, or malformed-body failure, not a negative result. Say so
  // rather than rendering "unverified", which would publish "not proven" for
  // every provider on the strength of a failed fetch.
  if (payload === null) return "unavailable";
  const state = payload.providers.find((row) => row.provider === provider)?.chips[chip]?.state;
  return state && KNOWN_STATES.has(state) ? state : "unverified";
}

export function certifiedChips(
  provider: GeneratedProviderId,
  covered: ProvenChips,
  payload: ProviderCertificationPayload | null,
): ProvenChips {
  const keys = Object.keys(covered) as ChipKey[];
  return Object.fromEntries(
    keys.map((chip) => [chip, chipCertification(provider, chip, covered, payload) === "certified"]),
  ) as ProvenChips;
}

export function useProviderCertification(): ProviderCertificationPayload | null {
  const [payload, setPayload] = useState<ProviderCertificationPayload | null>(null);
  useEffect(() => {
    let live = true;
    void fetchProviderCertification().then((result) => {
      if (live) setPayload(result);
    });
    return () => {
      live = false;
    };
  }, []);
  return payload;
}
