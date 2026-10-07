// GENERATED FILE - DO NOT EDIT.
// Source: server/zerg/config/managed_phase_contract.json
// Regenerate: make generate-phase-contract

// Phases an adapter may put on the wire; presence posts carry these.
export const WIRE_PRESENCE_STATES = ["thinking", "running", "blocked", "needs_user", "stalled", "idle"] as const;

export type WirePresenceState = (typeof WIRE_PRESENCE_STATES)[number];

export function isWirePresenceState(value: string | null | undefined): value is WirePresenceState {
  return (WIRE_PRESENCE_STATES as readonly string[]).includes(value ?? "");
}
