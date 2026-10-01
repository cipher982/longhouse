/**
 * Words for a machine's connection state, shared by the launch sheet and the
 * Machines page so the two never describe the same machine differently.
 */
import type { MachineDirectoryEntry } from "@/shared/api/index";

// "Console launch unavailable" is true and useless: it names the symptom a user
// already sees and withholds the cause, which the machine has been reporting
// all along. A connected machine that cannot launch is almost always one where
// a provider CLI is missing or signed out, and both are things the person
// sitting at that machine can fix in a minute -- if anyone tells them.
export function unreadyProviderLabel(machine: MachineDirectoryEntry): string | null {
  const readiness = machine.provider_readiness ?? {};
  const entries = Object.entries(readiness);
  if (entries.length === 0) return null;

  // Prefer whatever the user can act on. A signed-out CLI is one command away;
  // a missing one is an install. `unknown` is deliberately never surfaced as a
  // cause -- it means Longhouse could not ask, which is not the user's problem
  // to solve and would read as an accusation that they broke something.
  const signedOut = entries.filter(([, entry]) => entry?.state === "not_authenticated");
  const missing = entries.filter(([, entry]) => entry?.state === "cli_missing");
  const actionable = signedOut.length > 0 ? signedOut : missing;
  if (actionable.length === 0) return null;

  if (actionable.length === 1) {
    const [provider, entry] = actionable[0];
    return entry?.remediation ?? `${provider} is not ready`;
  }
  const verb = signedOut.length > 0 ? "Sign in to" : "Install";
  return `${verb} ${actionable.map(([provider]) => provider).sort().join(" or ")} on this machine`;
}

// A machine you are about to hand work to is a bet that it will still be there
// when the turn finishes. Uptime is how that bet is priced: a box connected for
// days is one thing, a box that connected forty seconds ago -- and has probably
// been reconnecting all morning -- is another. "Ready" alone hid the difference,
// and `connected_since` was tracked all along without ever being served.
export function onlineForLabel(machine: MachineDirectoryEntry): string {
  const since = machine.connected_since;
  if (!since) return "Ready";
  const started = new Date(since);
  if (Number.isNaN(started.getTime())) return "Ready";
  const elapsedMs = Date.now() - started.getTime();
  // A clock skewed into the future would otherwise render a negative age.
  if (elapsedMs < 0) return "Ready";
  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 1) return "Ready · just connected";
  if (minutes < 60) return `Ready · online ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Ready · online ${hours}h`;
  return `Ready · online ${Math.floor(hours / 24)}d`;
}

const RELATIVE_TIME = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

// "Last seen today" covered everything from one minute ago to twenty-three
// hours ago, which is the whole range a person actually cares about: a laptop
// that closed during lunch and one that has been shut since yesterday morning
// read identically. iOS already resolved to minutes here; this brings the web
// to the same answer rather than leaving two clients disagreeing about the
// same timestamp.
export function lastSeenLabel(machine: MachineDirectoryEntry): string {
  if (!machine.last_seen_at) return "Offline";
  const seen = new Date(machine.last_seen_at);
  if (Number.isNaN(seen.getTime())) return "Offline";
  const elapsedMs = Date.now() - seen.getTime();
  // A clock skewed into the future is not evidence of recent contact.
  if (elapsedMs < 0) return "Offline";

  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 1) return "Offline · Last seen just now";
  if (minutes < 60) return `Offline · Last seen ${RELATIVE_TIME.format(-minutes, "minute")}`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Offline · Last seen ${RELATIVE_TIME.format(-hours, "hour")}`;

  const days = Math.floor(hours / 24);
  return `Offline · Last seen ${RELATIVE_TIME.format(-days, "day")}`;
}

/**
 * One line for the Machines page: whether the Machine Agent is connected now,
 * and how long it has been, or when it was last heard from.
 */
export function connectionLabel(machine: MachineDirectoryEntry): string {
  if (!machine.online) return lastSeenLabel(machine);
  const since = machine.connected_since ? new Date(machine.connected_since).getTime() : Number.NaN;
  const elapsedMs = Date.now() - since;
  if (Number.isNaN(elapsedMs) || elapsedMs < 0) return "Online";
  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 1) return "Online · just connected";
  if (minutes < 60) return `Online · connected ${RELATIVE_TIME.format(-minutes, "minute")}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Online · connected ${RELATIVE_TIME.format(-hours, "hour")}`;
  return `Online · connected ${RELATIVE_TIME.format(-Math.floor(hours / 24), "day")}`;
}
