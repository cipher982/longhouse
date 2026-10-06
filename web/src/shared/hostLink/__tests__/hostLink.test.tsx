import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HOST_LINK_COPY } from "../copy";
import { HostLinkBanner } from "../HostLinkBanner";
import { HostLinkStore, type HostLifecycle } from "../store";

const pageCommit = "page-commit";
const claim = (overrides: Partial<HostLifecycle> = {}): HostLifecycle => ({
  type: "host.lifecycle",
  state: "updating",
  runtime_epoch: "runtime-1",
  attempt_id: "attempt-1",
  phase: "drain",
  expected_back_by: "2026-10-06T12:00:10.000Z",
  deadline: "2026-10-06T12:00:30.000Z",
  cutoff: "2026-10-06T12:00:40.000Z",
  ...overrides,
});

describe("host-link store", () => {
  it("gives serving evidence precedence over an active claim", () => {
    const store = new HostLinkStore({
      pageCommit,
      now: () => Date.parse("2026-10-06T12:00:00.000Z"),
    });
    store.observeLifecycle(claim());
    expect(store.getSnapshot().state).toBe("updating");

    store.observeSuccessfulWrite();
    expect(store.getSnapshot()).toMatchObject({ state: "serving", claim: null });

    store.observeLifecycle(claim());
    store.observeConnected({
      runtime_epoch: "runtime-open",
      admission: "open",
    });
    expect(store.getSnapshot()).toMatchObject({ state: "serving", claim: null });

    store.observeLifecycle(claim());
    store.observeLifecycle(claim({ state: "serving" }));
    expect(store.getSnapshot()).toMatchObject({ state: "serving", claim: null });
  });

  it("refetches for an epoch change without treating the epoch as serving evidence", async () => {
    const fetchHealth = vi
      .fn()
      .mockResolvedValueOnce({
        runtime: { epoch: "runtime-1", admission: "draining" as const },
      })
      .mockResolvedValueOnce({
        runtime: { epoch: "runtime-2", admission: "draining" as const },
      });
    const store = new HostLinkStore({
      pageCommit,
      now: () => Date.parse("2026-10-06T12:00:00.000Z"),
    });
    const stop = store.startMonitoring(fetchHealth);
    await store.refreshHealth();
    store.observeLifecycle(claim());
    store.observeConnected({ runtime_epoch: "runtime-2" });
    await store.refreshHealth();

    expect(fetchHealth).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot()).toMatchObject({
      state: "updating",
      runtimeEpoch: "runtime-2",
      claim: { attempt_id: "attempt-1" },
    });
    stop();
  });

  it("moves to slow_update at expected_back_by and unreachable at the deadline", () => {
    let now = Date.parse("2026-10-06T12:00:00.000Z");
    const store = new HostLinkStore({ pageCommit, now: () => now });
    store.observeLifecycle(claim());

    now += 9_999;
    store.refreshSnapshot();
    expect(store.getSnapshot().state).toBe("updating");

    now += 1;
    store.refreshSnapshot();
    expect(store.getSnapshot()).toMatchObject({
      state: "slow_update",
      elapsedSeconds: 10,
    });

    now = Date.parse("2026-10-06T12:00:30.000Z");
    store.refreshSnapshot();
    expect(store.getSnapshot().state).toBe("unreachable");
  });

  it("keeps the health endpoint polling every two seconds while a claim is held", async () => {
    vi.useFakeTimers();
    const fetchHealth = vi.fn(async () => ({
      runtime: { epoch: "runtime-1", admission: "draining" as const },
    }));
    const store = new HostLinkStore({
      pageCommit,
      now: () => Date.parse("2026-10-06T12:00:00.000Z"),
    });
    store.observeLifecycle(claim());
    const stop = store.startMonitoring(fetchHealth);
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchHealth).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(fetchHealth).toHaveBeenCalledTimes(2);

    stop();
    vi.useRealTimers();
  });
});

describe("host-link banner", () => {
  it("stays hidden during the first two seconds unless an action waits", () => {
    let now = Date.parse("2026-10-06T12:00:00.000Z");
    const store = new HostLinkStore({ pageCommit, now: () => now });
    store.observeLifecycle(claim());
    const { unmount } = render(<HostLinkBanner store={store} />);
    expect(screen.queryByTestId("host-link-banner")).not.toBeInTheDocument();

    now += 1_999;
    act(() => store.refreshSnapshot());
    expect(screen.queryByTestId("host-link-banner")).not.toBeInTheDocument();

    now += 1;
    act(() => store.refreshSnapshot());
    expect(screen.getByText(HOST_LINK_COPY.updatingBar)).toBeInTheDocument();
    unmount();

    const actionStore = new HostLinkStore({ pageCommit, now: () => now });
    actionStore.observeLifecycle(claim());
    const stopWaiting = actionStore.waitForServing(() => {});
    render(<HostLinkBanner store={actionStore} />);
    expect(screen.getByText(HOST_LINK_COPY.updatingBar)).toBeInTheDocument();
    stopWaiting();
  });

  it("shows the amber elapsed copy for slow_update", () => {
    let now = Date.parse("2026-10-06T12:00:00.000Z");
    const store = new HostLinkStore({ pageCommit, now: () => now });
    store.observeLifecycle(claim());
    now += 12_000;
    act(() => store.refreshSnapshot());
    render(<HostLinkBanner store={store} />);

    expect(screen.getByTestId("host-link-banner")).toHaveAttribute(
      "data-state",
      "slow_update",
    );
    expect(
      screen.getByText(`${HOST_LINK_COPY.slowUpdateHeadline} · 12s`),
    ).toBeInTheDocument();
  });

  it("offers reload only when the health build commit changed", () => {
    const store = new HostLinkStore({ pageCommit });
    const { rerender } = render(<HostLinkBanner store={store} />);

    act(() =>
      store.observeHealth({
        runtime: { epoch: "runtime-1", admission: "open" },
        build: { commit: pageCommit },
      }),
    );
    expect(screen.queryByTestId("host-link-banner")).not.toBeInTheDocument();

    act(() =>
      store.observeHealth({
        runtime: { epoch: "runtime-1", admission: "open" },
        build: { commit: "new-commit" },
      }),
    );
    rerender(<HostLinkBanner store={store} />);
    expect(screen.getByRole("button", { name: HOST_LINK_COPY.reload })).toBeInTheDocument();
  });
});

describe("host-link schema copy", () => {
  it("matches the canonical schema strings", () => {
    const schema = readFileSync(resolve(process.cwd(), "../schemas/host_link.yml"), "utf8");
    const keyForCopy: Record<keyof typeof HOST_LINK_COPY, string> = {
      updatingBar: "updating.web_bar",
      slowUpdateHeadline: "slow_update.headline",
      sendQueued: "send_queued",
      reload: "web_reload",
    };

    for (const [copyKey, schemaKey] of Object.entries(keyForCopy)) {
      const line = schema
        .split(/\r?\n/)
        .find((candidate) => candidate.trimStart().startsWith(`${schemaKey}:`));
      expect(line, `missing ${schemaKey} in schemas/host_link.yml`).toBeDefined();
      const value = line!.slice(line!.indexOf(":") + 1).trim();
      expect(HOST_LINK_COPY[copyKey as keyof typeof HOST_LINK_COPY]).toBe(value);
    }
  });
});
