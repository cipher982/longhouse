import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentSession } from "../../services/api/agents";
import {
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
} from "../sessionRuntime";
import { getRuntimeDisplayCopy } from "../sessionUtils";

type RuntimeExpectation = {
  management_label: "Managed" | "Unmanaged";
  status_label: string;
  status_tone: string;
  display_phase_label: string;
  runtime_headline: string;
  runtime_detail: string | null;
  runtime_tone: string;
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

describe.each(["basic-runtime-semantics.json", "divergent-matrix.json"])("shared runtime fixtures: %s", (fixtureName) => {
  const fixture = loadFixture(fixtureName);

  it.each(fixture.cases)("matches $name", ({ session, expectations }) => {
    const runtime = resolveSessionRuntimeState(session);
    const timelineCard = session.timeline_card ?? null;
    const timelineStatus = timelineCard?.status ?? {
      label: "No live signal",
      tone: "inactive",
      seen_at: null,
      seen_at_prefix: "Checked",
    };
    const runtimeDisplay = getRuntimeDisplayCopy(runtime, {
      managedLocal: true,
    });

    expect(timelineCard?.ownership.label ?? resolveSessionOwnershipLabel(runtime)).toBe(expectations.management_label);
    expect(timelineStatus.label).toBe(expectations.status_label);
    expect(timelineStatus.tone).toBe(expectations.status_tone);
    expect(runtime.displayPhase).toBe(expectations.display_phase_label);
    expect(runtimeDisplay.headline).toBe(expectations.runtime_headline);
    expect(runtimeDisplay.detail).toBe(expectations.runtime_detail);
    expect(runtime.tone).toBe(expectations.runtime_tone);
    expect(timelineStatus.seen_at ?? null).toBe(expectations.seen_at);
    expect(timelineStatus.seen_at_prefix).toBe(expectations.seen_at_prefix);
  });
});
