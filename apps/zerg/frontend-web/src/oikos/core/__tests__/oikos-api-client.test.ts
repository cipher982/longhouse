import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OikosAPIClient, type OikosEventData } from "../oikos-api-client";

type EventListener = (event: MessageEvent) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  options: EventSourceInit | undefined;
  listeners = new Map<string, EventListener[]>();
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();

  constructor(url: string, options?: EventSourceInit) {
    this.url = url;
    this.options = options;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type: string, payload: unknown) {
    const event = { data: JSON.stringify(payload) } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

describe("OikosAPIClient event stream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("routes automation and legacy update events through the automation-first handler surface", () => {
    const client = new OikosAPIClient("/base");
    const onAutomationUpdated = vi.fn<(event: OikosEventData) => void>();

    client.connectEventStream({ onAutomationUpdated });

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe("/base/api/oikos/events");
    expect(MockEventSource.instances[0].options).toEqual({ withCredentials: true });

    const automationEvent: OikosEventData = {
      type: "automation_updated",
      payload: { id: 42, status: "running" },
      timestamp: "2026-03-17T10:00:00Z",
    };
    MockEventSource.instances[0].emit("automation_updated", automationEvent);

    expect(onAutomationUpdated).toHaveBeenCalledTimes(1);
    expect(onAutomationUpdated).toHaveBeenLastCalledWith(automationEvent);

    const legacyEvent: OikosEventData = {
      type: "fiche_updated",
      payload: { id: 42, status: "success" },
      timestamp: "2026-03-17T10:01:00Z",
    };
    MockEventSource.instances[0].emit("fiche_updated", legacyEvent);

    expect(onAutomationUpdated).toHaveBeenCalledTimes(2);
    expect(onAutomationUpdated).toHaveBeenLastCalledWith(legacyEvent);
  });
});
