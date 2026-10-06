import type {
  HostHealthResponse,
  HostLifecycle,
  RuntimeAdmission,
} from "../../web/src/shared/hostLink/store";

export type HostLinkFixtureState = "updating" | "slow_update" | "reload";

export interface HostLinkFixture {
  health: HostHealthResponse & { status: "healthy" };
  connected: {
    stream_epoch: string;
    runtime_epoch: string;
    admission: RuntimeAdmission;
  };
  lifecycle: HostLifecycle | null;
}

export function buildHostLinkFixture(
  state: HostLinkFixtureState,
  nowMs = Date.now(),
): HostLinkFixture {
  const runtimeEpoch = `host-link-${state}-runtime`;
  const admission: RuntimeAdmission = state === "reload" ? "open" : "draining";
  const health: HostLinkFixture["health"] = {
    status: "healthy",
    runtime: { epoch: runtimeEpoch, admission },
    build: { commit: state === "reload" ? "fixture-updated-build" : "fixture-current-build" },
  };
  const connected = {
    stream_epoch: `host-link-${state}-stream`,
    runtime_epoch: runtimeEpoch,
    admission,
  };

  if (state === "reload") return { health, connected, lifecycle: null };

  const expectedBackBy =
    state === "slow_update" ? nowMs - 12_000 : nowMs + 30_000;
  const lifecycle: HostLifecycle = {
    type: "host.lifecycle",
    state: "updating",
    runtime_epoch: runtimeEpoch,
    attempt_id: `host-link-${state}-attempt`,
    phase: "drain",
    expected_back_by: new Date(expectedBackBy).toISOString(),
    deadline: new Date(nowMs + 120_000).toISOString(),
    cutoff: new Date(nowMs + 300_000).toISOString(),
  };
  return { health, connected, lifecycle };
}
