/**
 * `GET /api/public/provider-certification` as the landing page's provider
 * chart reads it, for `make ui-capture PAGE=landing SCENE=provider-certification`.
 *
 * Built from the generated capability contract so every provider and every
 * *covered* chip appears, then the states a real chart carries are dealt out:
 *
 * - most chips are certified (a current passing live test);
 * - Cursor is revoked: its last passes are still inside their window, but the
 *   cell failed twice in a row afterwards (a factory verdict with
 *   `consecutive_failures >= 2`), so the rows read `proof_status:
 *   infrastructure_error` and the chip is `unverified`, never `failing`;
 * - Claude's Resume is one failure after a pass, which changes nothing: it
 *   stays certified and only `latest_outcome` shows the failed run;
 * - Codex's Mid-turn pass aged out (`stale`).
 */
import { GENERATED_PROVIDER_CAPABILITIES } from "../../web/src/generated/provider-capabilities";

type ChipName = "search" | "launchAndSend" | "interrupt" | "steerMidTurn" | "resume";
type RowKind = "certified" | "revoked" | "single-failure" | "stale";

const GENERATED_AT = "2026-09-29T12:00:00Z";
const PASSED_AT = "2026-09-28T22:10:00Z";
const LONGHOUSE_SHA = "8c341d1d2b1f5a6c7e9d0a3b4c5d6e7f80912345";
const PROVIDER_VERSION: Record<string, string> = {
  claude: "2.1.140",
  codex: "0.148.0",
  cursor: "2026.09.24",
  omp: "14.2.0",
  opencode: "1.9.3",
  pi: "0.61.0",
};

function stateOf(kind: RowKind): string {
  if (kind === "revoked") return "unverified";
  if (kind === "stale") return "stale";
  return "certified";
}

function kindFor(provider: string, chip: ChipName): RowKind {
  if (provider === "cursor") return "revoked";
  if (provider === "claude" && chip === "resume") return "single-failure";
  if (provider === "codex" && chip === "steerMidTurn") return "stale";
  return "certified";
}

function requirement(provider: string, chip: ChipName, kind: RowKind) {
  const passed = kind === "certified" || kind === "single-failure";
  return {
    declared_in: chip === "resume" ? "session.resume.helm" : chip === "search" ? "session.transcript.search" : chip,
    scenario_id: `${provider}_helm_${chip.toLowerCase()}`,
    assertion_id: `${chip.toLowerCase()}_ok`,
    variant: null,
    proof_status: kind === "revoked" ? "infrastructure_error" : kind === "stale" ? "stale" : "pass",
    latest_outcome: kind === "revoked" || kind === "single-failure" ? "infrastructure_error" : "pass",
    proven_at: passed ? PASSED_AT : null,
    longhouse_git_sha: LONGHOUSE_SHA,
    provider_version: PROVIDER_VERSION[provider] ?? "1.0.0",
    accepted_epoch_id: "fixture-epoch",
    max_age_seconds: 604800,
  };
}

export function buildProviderCertificationFixture() {
  return {
    schema_version: 1,
    artifact_kind: "provider_chip_certification",
    certification_version: "provider-chip-certification-v1",
    generated_at: GENERATED_AT,
    providers: Object.values(GENERATED_PROVIDER_CAPABILITIES).map((capabilities) => ({
      provider: capabilities.id,
      chips: Object.fromEntries(
        (Object.keys(capabilities.proven) as ChipName[]).map((chip) => {
          if (!capabilities.proven[chip]) return [chip, { state: "unproven", requirements: [] }];
          const kind = kindFor(capabilities.id, chip);
          return [chip, { state: stateOf(kind), requirements: [requirement(capabilities.id, chip, kind)] }];
        }),
      ),
    })),
  };
}
