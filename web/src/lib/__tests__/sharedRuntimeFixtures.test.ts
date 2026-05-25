import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentSession } from "../../services/api/agents";
import {
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
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

function loadFixture(name: string): RuntimeFixture {
  const fixturePath = resolve(process.cwd(), "../tests/fixtures/session-runtime", name);
  return JSON.parse(readFileSync(fixturePath, "utf8")) as RuntimeFixture;
}

describe("shared runtime fixtures", () => {
  const fixture = loadFixture("basic-runtime-semantics.json");

  it.each(fixture.cases)("matches $name", ({ session, expectations }) => {
    const runtime = resolveSessionRuntimeState(session);
    const timelineCard = session.timeline_card ?? null;
    const timelineStatus = timelineCard?.status ?? {
      label: "No live signal",
      tone: "inactive",
      seen_at: null,
      seen_at_prefix: "Checked",
    };

    expect(timelineCard?.ownership.label ?? resolveSessionOwnershipLabel(runtime)).toBe(expectations.management_label);
    expect(timelineStatus.label).toBe(expectations.status_label);
    expect(timelineStatus.tone).toBe(expectations.status_tone);
    expect(runtime.displayPhase).toBe(expectations.display_phase_label);
    expect(timelineStatus.seen_at ?? null).toBe(expectations.seen_at);
    expect(timelineStatus.seen_at_prefix).toBe(expectations.seen_at_prefix);
  });
});
