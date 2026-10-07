/**
 * Presentation helpers for the Machines page and the machine page. The status
 * words and tone themselves are served (`MachineSummary.status`, decided in
 * server/zerg/services/machine_status.py) so every client shows the same line;
 * the contract is control-plane/docs/specs/machines-surface.md.
 */
import type { MachineActivity, MachineDirectoryEntry, MachineSummary, MachineSync } from "@/shared/api/index";
import { getProviderLabel } from "@/shared/lib/providers";
import { parseUTC } from "@/shared/lib/dateUtils";

export type MachineStatus = MachineSummary["status"];
export type MachineTone = MachineStatus["tone"];

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
  return seen ? `last seen ${seen}` : "No connection evidence";
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
  // Only a disconnected machine, which reports no agents at all, falls back to
  // what it ran; a connected one shows exactly what it reports.
  if (!machine.online) {
    return [...usage.keys()].map((provider) => ({ provider, unavailable: false }));
  }
  return [
    ...launchable.map((provider) => ({ provider, unavailable: false })),
    ...unavailable.map((provider) => ({ provider, unavailable: true })),
  ];
}

const IMPORTING_STATES: Record<string, true> = { discovering: true, inventory_ready: true, importing: true, backpressured: true };

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
  if (history.state === "current") {
    return { text: files ? `All imported · ${files}` : "All imported", tone: "live" };
  }
  if (history.state === "discovering") return { text: "Discovering history", tone: "plain" };
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
  return Boolean(IMPORTING_STATES[history.state]);
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
