export type HostLinkState = "serving" | "updating" | "slow_update" | "unreachable";
export type RuntimeAdmission = "open" | "pending" | "draining";

export interface HostLifecycle {
  type?: "host.lifecycle";
  state: "updating" | "serving";
  runtime_epoch: string;
  attempt_id?: string | null;
  phase?: string | null;
  expected_back_by?: string | null;
  deadline?: string | null;
  cutoff?: string | null;
}

export interface HostHealthResponse {
  runtime?: { epoch?: string; admission?: RuntimeAdmission };
  build?: { commit?: string };
}

export interface HostLinkSnapshot {
  state: HostLinkState;
  claim: HostLifecycle | null;
  claimStartedAtMs: number | null;
  runtimeEpoch: string | null;
  admission: RuntimeAdmission | null;
  pageCommit: string | null;
  buildCommit: string | null;
  actionWaiting: boolean;
  showUpdating: boolean;
  reloadAvailable: boolean;
  elapsedSeconds: number;
}

export type HostHealthFetcher = () => Promise<HostHealthResponse>;

function pageBuildCommit(): string | null {
  if (typeof document === "undefined") return null;
  const commit = document.querySelector<HTMLMetaElement>(
    'meta[name="longhouse-build-commit"]',
  )?.content.trim();
  return commit || null;
}

function timestampMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function effectiveClaimDeadline(claim: HostLifecycle): number | null {
  const deadline = timestampMs(claim.deadline);
  const cutoff = timestampMs(claim.cutoff);
  if (deadline === null) return cutoff;
  if (cutoff === null) return deadline;
  return Math.min(deadline, cutoff);
}

export class HostLinkStore {
  private readonly listeners = new Set<() => void>();
  private readonly servingWaiters = new Set<() => void>();
  private readonly now: () => number;
  private readonly pageCommit: string | null;
  private claim: HostLifecycle | null = null;
  private claimStartedAtMs: number | null = null;
  private runtimeEpoch: string | null = null;
  private admission: RuntimeAdmission | null = null;
  private buildCommit: string | null = null;
  private servingEvidence = false;
  private lastServingEpoch: string | null = null;
  private healthFetcher: HostHealthFetcher | null = null;
  private healthRequest: Promise<void> | null = null;
  private monitorCount = 0;
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private healthTimer: ReturnType<typeof setTimeout> | null = null;
  private snapshot: HostLinkSnapshot;

  constructor(options: { pageCommit?: string | null; now?: () => number } = {}) {
    this.pageCommit = options.pageCommit === undefined ? pageBuildCommit() : options.pageCommit;
    this.now = options.now ?? Date.now;
    this.snapshot = this.deriveSnapshot();
  }

  readonly getSnapshot = (): HostLinkSnapshot => this.snapshot;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };


  startMonitoring(fetchHealth: HostHealthFetcher): () => void {
    this.healthFetcher = fetchHealth;
    this.monitorCount += 1;
    if (this.monitorCount === 1) {
      this.tickTimer = setInterval(() => this.refreshSnapshot(), 1_000);
      void this.refreshHealth();
      this.syncHealthPoll();
    }

    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.monitorCount = Math.max(0, this.monitorCount - 1);
      if (this.monitorCount > 0) return;
      clearInterval(this.tickTimer ?? undefined);
      this.tickTimer = null;
      clearTimeout(this.healthTimer ?? undefined);
      this.healthTimer = null;
      this.healthFetcher = null;
    };
  }

  refreshHealth(): Promise<void> {
    if (!this.healthFetcher) return Promise.resolve();
    if (this.healthRequest) return this.healthRequest;

    this.healthRequest = this.healthFetcher()
      .then((health) => this.observeHealth(health))
      .catch(() => this.observeHealthFailure())
      .finally(() => {
        this.healthRequest = null;
        this.syncHealthPoll();
      });
    return this.healthRequest;
  }

  observeHealth(health: HostHealthResponse): void {
    const epoch = health.runtime?.epoch || null;
    const admission = health.runtime?.admission || null;
    const buildCommit = health.build?.commit || null;
    if (epoch) this.runtimeEpoch = epoch;
    if (admission) this.admission = admission;
    if (buildCommit) this.buildCommit = buildCommit;

    if (admission === "open") {
      this.markServing(epoch);
    } else if (admission === "pending" || admission === "draining") {
      this.servingEvidence = false;
    }

    this.refreshSnapshot();
  }

  observeConnected(input: {
    runtime_epoch?: string | null;
    admission?: RuntimeAdmission | null;
  }): void {
    const previousEpoch = this.runtimeEpoch;
    if (input.runtime_epoch) this.runtimeEpoch = input.runtime_epoch;
    if (input.admission) this.admission = input.admission;

    if (input.admission === "open") {
      this.markServing(input.runtime_epoch ?? null);
    } else if (input.admission === "pending" || input.admission === "draining") {
      this.servingEvidence = false;
    }

    this.refreshSnapshot();
    if (
      input.runtime_epoch &&
      previousEpoch &&
      input.runtime_epoch !== previousEpoch
    ) {
      void this.refreshHealth();
    }
  }

  observeLifecycle(lifecycle: HostLifecycle): void {
    if (lifecycle.state === "serving") {
      this.runtimeEpoch = lifecycle.runtime_epoch || this.runtimeEpoch;
      this.markServing(lifecycle.runtime_epoch || null);
      this.refreshSnapshot();
      void this.refreshHealth();
      return;
    }

    const isNewClaim =
      !this.claim ||
      (this.claim.attempt_id != null &&
        lifecycle.attempt_id != null &&
        this.claim.attempt_id !== lifecycle.attempt_id);
    this.servingEvidence = false;
    this.runtimeEpoch = lifecycle.runtime_epoch || this.runtimeEpoch;
    this.claim = lifecycle;
    if (isNewClaim || this.claimStartedAtMs === null) {
      this.claimStartedAtMs = this.now();
    }
    this.refreshSnapshot();
  }

  observeApiError(status: number, body: unknown): void {
    if (!body || typeof body !== "object") return;
    const response = body as {
      code?: unknown;
      runtime_epoch?: unknown;
      admission?: unknown;
      claim?: unknown;
    };
    if (typeof response.runtime_epoch === "string") {
      this.runtimeEpoch = response.runtime_epoch;
    }
    if (
      response.admission === "open" ||
      response.admission === "pending" ||
      response.admission === "draining"
    ) {
      this.admission = response.admission;
    }

    if (status === 503 && response.code === "runtime_restarting") {
      if (
        response.claim &&
        typeof response.claim === "object" &&
        (response.claim as HostLifecycle).state === "updating"
      ) {
        this.observeLifecycle(response.claim as HostLifecycle);
      } else {
        this.servingEvidence = false;
        this.refreshSnapshot();
      }
      return;
    }

    if (status === 503 && response.code === "runtime_unreachable") {
      this.servingEvidence = false;
      this.claim = null;
      this.claimStartedAtMs = null;
      this.refreshSnapshot();
    }
  }

  observeSuccessfulWrite(): void {
    const wasServing = this.snapshot.state === "serving";
    this.markServing(this.runtimeEpoch);
    this.refreshSnapshot();
    if (!wasServing) void this.refreshHealth();
  }

  observeHealthFailure(): void {
    if (!this.claim) this.servingEvidence = false;
    this.refreshSnapshot();
  }

  waitForServing(onServing: () => void): () => void {
    let active = true;
    const cancel = () => {
      if (!active) return;
      active = false;
      this.servingWaiters.delete(ready);
      this.refreshSnapshot();
    };
    const ready = () => {
      if (!active) return;
      cancel();
      onServing();
    };

    if (this.snapshot.state === "serving") {
      queueMicrotask(ready);
    } else {
      this.servingWaiters.add(ready);
      this.refreshSnapshot();
      this.syncHealthPoll();
    }
    return cancel;
  }

  /** Recompute time-derived state; used by the monitor and deterministic tests. */
  refreshSnapshot(): void {
    const next = this.deriveSnapshot();
    if (!this.sameSnapshot(this.snapshot, next)) {
      this.snapshot = next;
      for (const listener of this.listeners) listener();
    }
    this.syncHealthPoll();

    if (next.state === "serving" && this.servingWaiters.size > 0) {
      const waiters = Array.from(this.servingWaiters);
      for (const waiter of waiters) waiter();
    }
  }

  private deriveSnapshot(): HostLinkSnapshot {
    const now = this.now();
    let state: HostLinkState = "unreachable";
    if (this.servingEvidence) {
      state = "serving";
    } else if (this.claim) {
      const deadline = effectiveClaimDeadline(this.claim);
      if (deadline !== null && now >= deadline) {
        state = "unreachable";
      } else {
        const expectedBack = timestampMs(this.claim.expected_back_by);
        state = expectedBack !== null && now >= expectedBack ? "slow_update" : "updating";
      }
    }

    const elapsedSeconds =
      this.claimStartedAtMs === null
        ? 0
        : Math.max(0, Math.floor((now - this.claimStartedAtMs) / 1_000));
    const actionWaiting = this.servingWaiters.size > 0;
    const reloadAvailable =
      state === "serving" &&
      Boolean(this.pageCommit && this.buildCommit && this.pageCommit !== this.buildCommit);

    return {
      state,
      claim: this.claim,
      claimStartedAtMs: this.claimStartedAtMs,
      runtimeEpoch: this.runtimeEpoch,
      admission: this.admission,
      pageCommit: this.pageCommit,
      buildCommit: this.buildCommit,
      actionWaiting,
      showUpdating: state === "updating" && (elapsedSeconds >= 2 || actionWaiting),
      reloadAvailable,
      elapsedSeconds,
    };
  }

  private markServing(epoch: string | null): void {
    this.servingEvidence = true;
    this.claim = null;
    this.claimStartedAtMs = null;
    this.admission = "open";
    if (!epoch) return;
    this.runtimeEpoch = epoch;
    const previousServingEpoch = this.lastServingEpoch;
    this.lastServingEpoch = epoch;
    if (previousServingEpoch && previousServingEpoch !== epoch) {
      this.requestViewRefresh(epoch);
    }
  }

  private requestViewRefresh(epoch: string): void {
    if (typeof window === "undefined") return;
    window.dispatchEvent(
      new CustomEvent("longhouse:host-link-epoch-changed", { detail: { epoch } }),
    );
  }

  private sameSnapshot(a: HostLinkSnapshot, b: HostLinkSnapshot): boolean {
    return (
      a.state === b.state &&
      a.claim === b.claim &&
      a.claimStartedAtMs === b.claimStartedAtMs &&
      a.runtimeEpoch === b.runtimeEpoch &&
      a.admission === b.admission &&
      a.pageCommit === b.pageCommit &&
      a.buildCommit === b.buildCommit &&
      a.actionWaiting === b.actionWaiting &&
      a.showUpdating === b.showUpdating &&
      a.reloadAvailable === b.reloadAvailable &&
      a.elapsedSeconds === b.elapsedSeconds
    );
  }

  private syncHealthPoll(): void {
    if (this.monitorCount === 0 || !this.healthFetcher) return;
    const shouldPoll =
      this.snapshot.state === "updating" ||
      this.snapshot.state === "slow_update" ||
      this.servingWaiters.size > 0;

    if (!shouldPoll) {
      clearTimeout(this.healthTimer ?? undefined);
      this.healthTimer = null;
      return;
    }
    if (this.healthTimer !== null) return;

    this.healthTimer = setTimeout(() => {
      this.healthTimer = null;
      void this.refreshHealth();
    }, 2_000);
  }
}

export const hostLinkStore = new HostLinkStore();


export function startHostLinkMonitoring(fetchHealth: HostHealthFetcher): () => void {
  return hostLinkStore.startMonitoring(fetchHealth);
}

export function observeHostLifecycle(lifecycle: HostLifecycle): void {
  hostLinkStore.observeLifecycle(lifecycle);
}

export function observeHostLinkConnected(input: {
  runtime_epoch?: string | null;
  admission?: RuntimeAdmission | null;
}): void {
  hostLinkStore.observeConnected(input);
}

export function observeHostLinkApiError(status: number, body: unknown): void {
  hostLinkStore.observeApiError(status, body);
}

export function observeHostLinkSuccessfulWrite(): void {
  hostLinkStore.observeSuccessfulWrite();
}
