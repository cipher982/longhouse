/**
 * One vocabulary for a machine's state on the Machines page, the machine page
 * and the nav. iOS mirrors this table (ios/Sources/LonghouseApp/Machines/MachineStatus.swift);
 * the contract is control-plane/docs/specs/machines-surface.md.
 *
 * Three independent axes feed it and none is inferred from another: the live
 * control connection (`machine.online`, `launch`), what the Timeline shows as
 * live on that machine (`activity.live_count`), and whether the Machine Agent
 * is shipping (`sync`). Ordinary offline is gray, never red: red is reserved for
 * a fault someone has to repair.
 */
import type { MachineActivity, MachineDirectoryEntry, MachineSync } from "@/shared/api/index";
import { getProviderLabel } from "@/shared/lib/providers";
import { parseUTC } from "@/shared/lib/dateUtils";

export type MachineTone = "live" | "attention" | "fault" | "quiet" | "idle" | "off";

export type MachineStatus = {
  tone: MachineTone;
  /** Short coloured status, e.g. "9 live", "Codex signed out", "Offline". */
  label: string;
  /** One line a person can act on, or null. */
  hint: string | null;
  /** Machines nobody has heard from, folded below the list. */
  quiet: boolean;
};

type StatusInput = {
  machine: MachineDirectoryEntry;
  activity?: Pick<MachineActivity, "live_count" | "sessions_started"> | null;
  sync?: Pick<MachineSync, "status" | "stale"> | null;
};

const REPAIR_REASONS: Record<string, true> = { auth_failed: true, runtime_unreachable: true };

function signInNeeds(machine: MachineDirectoryEntry): { label: string; hint: string } | null {
  const unavailable = machine.launch.unavailable_providers ?? [];
  const signedOut = unavailable.filter((item) => item.reason === "not_authenticated");
  // A missing CLI only matters when it leaves nothing to start sessions with;
  // otherwise it is an agent this person simply does not use.
  const missing = machine.launch.providers.length === 0 ? unavailable.filter((item) => item.reason === "cli_missing") : [];
  const actionable = signedOut.length > 0 ? signedOut : missing;
  if (actionable.length === 0) return null;
  const verb = signedOut.length > 0 ? "signed out" : "not installed";
  if (actionable.length === 1) {
    const item = actionable[0];
    const name = getProviderLabel(item.provider);
    return {
      label: `${name} ${verb}`,
      hint: item.remediation ?? (signedOut.length > 0 ? `Sign in to ${name} on ${machine.machine_name}` : `Install ${name} on ${machine.machine_name}`),
    };
  }
  const names = actionable.map((item) => getProviderLabel(item.provider)).sort();
  return {
    label: `${actionable.length} agents ${verb}`,
    hint: `${signedOut.length > 0 ? "Sign in to" : "Install"} ${names.join(" and ")} on ${machine.machine_name}`,
  };
}

export function machineStatus({ machine, activity, sync }: StatusInput): MachineStatus {
  const live = activity?.live_count ?? 0;
  const started = activity?.sessions_started ?? 0;
  const blocked = machine.launch.blocked_by ?? null;
  const syncFresh = Boolean(sync && !sync.stale);
  const quietBase = { hint: null, quiet: false };

  if ((blocked && REPAIR_REASONS[blocked]) || (syncFresh && sync?.status === "broken")) {
    return { tone: "fault", label: "Needs repair", hint: "Run longhouse machine repair on this machine", quiet: false };
  }
  if (machine.online) {
    const needs = signInNeeds(machine);
    if (live > 0) return { tone: "live", label: `${live} live`, hint: needs?.hint ?? null, quiet: false };
    if (needs) return { tone: "attention", label: needs.label, hint: needs.hint, quiet: false };
    if (blocked === "engine_too_old") {
      return { tone: "attention", label: "Update required", hint: "Update Longhouse on this machine", quiet: false };
    }
    if (blocked === "no_launch_support") return { tone: "attention", label: "Can't start sessions", ...quietBase };
    return { tone: "idle", label: "Online, idle", ...quietBase };
  }
  if (live > 0) return { tone: "live", label: `${live} live`, ...quietBase };
  if (syncFresh) return { tone: "quiet", label: "Sync only", ...quietBase };
  return { tone: "off", label: "Offline", hint: null, quiet: started === 0 };
}

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

/** "just now", "3 minutes ago", "2 hours ago", "9 days ago"; null when unknown. */
export function relativeTime(value: string | null | undefined, now = Date.now()): string | null {
  if (!value) return null;
  const at = parseUTC(value).getTime();
  if (Number.isNaN(at)) return null;
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return RELATIVE.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return RELATIVE.format(-hours, "hour");
  return RELATIVE.format(-Math.round(hours / 24), "day");
}

/** "12h 32m", "3d 4h"; null when unknown. */
export function durationSince(value: string | null | undefined, now = Date.now()): string | null {
  if (!value) return null;
  const at = parseUTC(value).getTime();
  if (Number.isNaN(at) || at > now) return null;
  const minutes = Math.floor((now - at) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** Second line under a machine's name: how long it has held its connection, or when it last had one. */
export function connectionLine(machine: MachineDirectoryEntry, now = Date.now()): string {
  if (machine.online) {
    const held = durationSince(machine.connected_since, now);
    return held && held !== "just now" ? `online for ${held}` : "just connected";
  }
  const seen = relativeTime(machine.last_seen_at, now);
  return seen ? `last connected ${seen}` : "never connected";
}

// Chart colours per provider, tuned for the dark ground: several brand
// colours (Cursor's near-black, Codex's cream) vanish as bar fills. iOS uses
// the same map.
const ACTIVITY_COLORS: Record<string, string> = {
  omp: "#A35BF5",
  claude: "#D97757",
  codex: "#7FB4FF",
  cursor: "#BDB3A0",
  opencode: "#9AA3A8",
  pi: "#F1BE58",
  antigravity: "#4F87ED",
  zai: "#B06E8A",
};

export function activityColor(provider: string): string {
  return ACTIVITY_COLORS[provider.toLowerCase()] ?? "#8A7862";
}

/** Providers by sessions in the window, most used first. */
export function providerTotals(activity: Pick<MachineActivity, "daily"> | null | undefined): Array<[string, number]> {
  const totals = new Map<string, number>();
  for (const day of activity?.daily ?? []) {
    for (const [provider, count] of Object.entries(day.by_provider ?? {})) {
      totals.set(provider, (totals.get(provider) ?? 0) + count);
    }
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/**
 * The agents to show beside a machine: the ones it can start now, most used
 * first, then the ones it reports as signed out or missing (dimmed). An offline
 * machine reports none, so it shows the agents it ran in the window.
 */
export function machineAgents(
  machine: MachineDirectoryEntry,
  activity: Pick<MachineActivity, "daily"> | null | undefined,
): Array<{ provider: string; unavailable: boolean }> {
  const usage = new Map(providerTotals(activity));
  const byUsage = (a: string, b: string) => (usage.get(b) ?? 0) - (usage.get(a) ?? 0) || a.localeCompare(b);
  const launchable = machine.launch.providers.map((option) => option.provider).sort(byUsage);
  const unavailable = (machine.launch.unavailable_providers ?? [])
    .filter((item) => item.reason === "not_authenticated")
    .map((item) => item.provider)
    .filter((provider) => !launchable.includes(provider))
    .sort(byUsage);
  if (launchable.length === 0 && unavailable.length === 0) {
    return [...usage.keys()].map((provider) => ({ provider, unavailable: false }));
  }
  return [
    ...launchable.map((provider) => ({ provider, unavailable: false })),
    ...unavailable.map((provider) => ({ provider, unavailable: true })),
  ];
}

const IMPORTING_STATES: Record<string, true> = { discovering: true, inventory_ready: true, importing: true, backpressured: true };
// Below this the import is a rounding error a person should not be told about.
const IMPORT_NOTICE_BYTES = 64 * 1024;

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** History import in words; null while there is nothing worth saying. */
export function historyLine(sync: MachineSync | null | undefined): { text: string; tone: "live" | "attention" | "fault" | "plain" } {
  if (!sync) return { text: "No reports yet", tone: "plain" };
  const history = sync.history;
  const remaining = history.remaining_bytes ?? 0;
  const files = history.source_count != null ? `${history.source_count.toLocaleString()} files` : null;
  if (history.state === "current" || (IMPORTING_STATES[history.state] && remaining <= IMPORT_NOTICE_BYTES && (history.remaining_records ?? 0) === 0)) {
    return { text: files ? `All imported · ${files}` : "All imported", tone: "live" };
  }
  if (IMPORTING_STATES[history.state]) {
    return { text: remaining > 0 ? `Importing · ${formatBytes(remaining)} left` : "Importing", tone: "plain" };
  }
  if (history.state === "paused") return { text: "Paused", tone: "plain" };
  if (history.state === "blocked_source") return { text: "Blocked on a source file", tone: "attention" };
  if (history.state === "offline") return { text: "Machine offline", tone: "plain" };
  return { text: "Not reported", tone: "plain" };
}

export function isImporting(sync: MachineSync | null | undefined): boolean {
  if (!sync || sync.stale) return false;
  const history = sync.history;
  return Boolean(IMPORTING_STATES[history.state]) && ((history.remaining_bytes ?? 0) > IMPORT_NOTICE_BYTES || (history.remaining_records ?? 0) > 0);
}

type RunnerLike = { id: number; name: string; status: string; availability_policy?: string | null };

/**
 * A Runner is optional shell-command tooling, not a machine. It belongs to the
 * machine whose name it carries; nothing else ties the two identities together.
 * Revoked runners never reconnect and are not shown.
 */
export function runnerForMachine<R extends RunnerLike>(runners: R[], machine: MachineDirectoryEntry): R | null {
  return runners.find((runner) => runner.status !== "revoked" && (runner.name === machine.machine_name || runner.name === machine.device_id)) ?? null;
}

/** Runners with no machine of the same name, minus revoked ones and ephemeral ones that have gone away. */
export function unmatchedRunners<R extends RunnerLike>(runners: R[], machines: MachineDirectoryEntry[]): R[] {
  const names: Record<string, true> = {};
  for (const machine of machines) {
    names[machine.machine_name] = true;
    names[machine.device_id] = true;
  }
  return runners.filter(
    (runner) =>
      !names[runner.name] &&
      runner.status !== "revoked" &&
      (runner.status === "online" || runner.availability_policy !== "ephemeral"),
  );
}
