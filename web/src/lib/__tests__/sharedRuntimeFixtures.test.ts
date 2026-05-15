import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentSession } from "../../services/api/agents";
import {
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
  resolveSessionStatusLabel,
} from "../sessionRuntime";

type RuntimeExpectation = {
  management_label: "Managed" | "Unmanaged";
  status_label: string;
  status_tone: string;
  display_phase_label: string;
  seen_at: string | null;
  seen_at_prefix: string;
};

type RuntimeFixtureCase = {
  name: string;
  session: AgentSession;
  expectations: RuntimeExpectation;
};

type RuntimeFixture = {
  name: string;
  cases: RuntimeFixtureCase[];
};

function shouldUseTimelineCardStatus(session: AgentSession): boolean {
  const timelineCardStatus = session.timeline_card?.status ?? null;
  const runtimeFacts = session.runtime_facts ?? null;
  const processState = runtimeFacts?.process_state ?? null;
  const phaseKind = runtimeFacts?.phase?.kind?.trim() || null;
  const hasProcessAxis = processState === "running" || processState === "closed" || processState === "unknown";
  const processOnly =
    hasProcessAxis &&
    phaseKind == null &&
    timelineCardStatus != null &&
    (timelineCardStatus.tone === "inactive" || timelineCardStatus.tone === "closed");
  return timelineCardStatus != null && !processOnly;
}

function loadFixture(name: string): RuntimeFixture {
  const fixturePath = resolve(process.cwd(), "../tests/fixtures/session-runtime", name);
  return JSON.parse(readFileSync(fixturePath, "utf8")) as RuntimeFixture;
}

describe("shared runtime fixtures", () => {
  const fixture = loadFixture("basic-runtime-semantics.json");

  it.each(fixture.cases)("matches $name", ({ session, expectations }) => {
    const runtime = resolveSessionRuntimeState(session);
    const timelineCard = session.timeline_card ?? null;
    const timelineStatus = shouldUseTimelineCardStatus(session) ? (timelineCard?.status ?? null) : null;
    const fallbackControlPath = expectations.management_label === "Managed" ? "managed" : "unmanaged";
    const statusLabel = timelineStatus?.label ?? resolveSessionStatusLabel(runtime, fallbackControlPath);
    const statusTone = timelineStatus?.tone ?? timelineCard?.border_tone ?? runtime.tone;
    const seenAt = timelineStatus?.seen_at ?? runtime.factStatus?.seenAt ?? null;
    const seenAtPrefix = timelineStatus?.seen_at_prefix ?? runtime.factStatus?.seenAtPrefix ?? "Updated";

    expect(timelineCard?.ownership.label ?? resolveSessionOwnershipLabel(runtime)).toBe(expectations.management_label);
    expect(statusLabel).toBe(expectations.status_label);
    expect(statusTone).toBe(expectations.status_tone);
    expect(runtime.displayPhase).toBe(expectations.display_phase_label);
    expect(seenAt).toBe(expectations.seen_at);
    expect(seenAtPrefix).toBe(expectations.seen_at_prefix);
  });
});
