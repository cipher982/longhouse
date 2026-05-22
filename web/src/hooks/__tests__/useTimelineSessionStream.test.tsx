import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTimelineSessionStream } from "../useTimelineSessionStream";
import type { AgentSessionFilters } from "../../services/api/agents";

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  options: EventSourceInit | undefined;
  close = vi.fn();

  constructor(url: string, options?: EventSourceInit) {
    this.url = url;
    this.options = options;
    MockEventSource.instances.push(this);
  }

  addEventListener = vi.fn();
}

function Harness({
  filters,
  options,
}: {
  filters: AgentSessionFilters;
  options: { enabled?: boolean; skipInitialReplay?: boolean };
}) {
  useTimelineSessionStream(filters, options);
  return null;
}

function renderStream(filters: AgentSessionFilters, options: { enabled?: boolean; skipInitialReplay?: boolean }) {
  const queryClient = new QueryClient();
  return {
    queryClient,
    view: render(
      <QueryClientProvider client={queryClient}>
        <Harness filters={filters} options={options} />
      </QueryClientProvider>,
    ),
  };
}

describe("useTimelineSessionStream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not reconnect when only the one-shot initial replay flag changes", () => {
    const filters = { project: "zerg", provider: "codex", days_back: 14, limit: 50 };
    const { queryClient, view } = renderStream(filters, { enabled: true, skipInitialReplay: true });

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toContain("skip_initial_replay=true");

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <Harness filters={filters} options={{ enabled: true, skipInitialReplay: false }} />
      </QueryClientProvider>,
    );

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].close).not.toHaveBeenCalled();
  });
});
