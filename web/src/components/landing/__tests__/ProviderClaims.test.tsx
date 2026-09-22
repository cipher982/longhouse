import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import {
  GENERATED_PROVIDER_CAPABILITIES,
  type GeneratedProviderId,
  type ProvenChips,
} from "../../../generated/provider-capabilities";
import { lookupProviderBrand } from "../../../generated/provider-brands";
import {
  resetProviderCertificationCache,
  type CertificationState,
  type ChipKey,
  type ProviderCertificationPayload,
} from "../../../lib/providerCertification";

// The rails render generated contract data joined to the served
// certification, so expectations are derived from that same contract rather
// than transcribed. A hand-typed provider table here would only detect change.
const IDS = Object.keys(GENERATED_PROVIDER_CAPABILITIES) as GeneratedProviderId[];
const CHIP_ORDER: ChipKey[] = ["search", "launchAndSend", "interrupt", "steerMidTurn", "resume"];

function payload(stateFor: (id: GeneratedProviderId, chip: ChipKey) => CertificationState): ProviderCertificationPayload {
  return {
    artifact_kind: "provider_chip_certification",
    generated_at: "2026-09-16T12:00:00Z",
    providers: IDS.map((id) => ({
      provider: id,
      chips: Object.fromEntries(
        CHIP_ORDER.map((chip) => [chip, { state: stateFor(id, chip), requirements: [] }]),
      ),
    })),
  };
}

function serve(body: ProviderCertificationPayload | null) {
  resetProviderCertificationCache();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      body === null ? new Response("unavailable", { status: 503 }) : new Response(JSON.stringify(body), { status: 200 }),
    ),
  );
}

function renderRails() {
  render(
    <MemoryRouter>
      <IntegrationsSection />
    </MemoryRouter>,
  );
  const rails = screen.getAllByRole("listitem");
  return (id: GeneratedProviderId) => {
    const name = lookupProviderBrand(id).marketingName;
    const rail = rails.find((item) => item.querySelector(".landing-provider-row-name")?.textContent === name);
    expect(rail, `no landing rail for ${id} (${name})`).toBeDefined();
    return rail!;
  };
}

const chipsOf = (rail: Element, attribute: string) =>
  Array.from(rail.querySelectorAll(".landing-provider-capability")).map((chip) => chip.getAttribute(attribute));

afterEach(() => {
  vi.unstubAllGlobals();
  resetProviderCertificationCache();
});

describe("landing provider claims", () => {
  it("reports unavailable rather than unproven when certification cannot be read", async () => {
    serve(null);
    const railFor = renderRails();
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    expect(screen.queryByText("Sync, timeline, and full-text search")).toBeNull();
    for (const id of IDS) {
      const covered = GENERATED_PROVIDER_CAPABILITIES[id].proven;
      const rail = railFor(id);
      expect(chipsOf(rail, "data-supported")).toEqual(CHIP_ORDER.map(() => "false"));
      expect(chipsOf(rail, "data-certification")).toEqual(
        CHIP_ORDER.map((chip) => (covered[chip] ? "unavailable" : "unproven")),
      );
    }
    // A failed load must not be published as a negative product claim.
    expect(screen.queryAllByText(/not currently proven/)).toHaveLength(0);
    expect(screen.getAllByText("Certification status is unavailable right now.")).toHaveLength(IDS.length);
  });

  it("does not expose internal control bookkeeping in public claims", async () => {
    serve(payload(() => "unverified"));
    const railFor = renderRails();
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    for (const id of IDS) {
      const covered = GENERATED_PROVIDER_CAPABILITIES[id].proven;
      expect(chipsOf(railFor(id), "data-certification")).toEqual(
        CHIP_ORDER.map((chip) => (covered[chip] ? "unverified" : "unproven")),
      );
    }
    expect(screen.queryAllByText(/negative controls|controls_pending/)).toHaveLength(0);
  });


  it("a certification for an uncovered chip never lights it", async () => {
    serve(payload(() => "certified"));
    const railFor = renderRails();
    for (const id of IDS) {
      const covered = GENERATED_PROVIDER_CAPABILITIES[id].proven;
      await waitFor(() =>
        expect(chipsOf(railFor(id), "data-supported")).toEqual(CHIP_ORDER.map((chip) => String(covered[chip]))),
      );
    }
  });

  it("claims in each summary follow the certified chips, not the covered ones", async () => {
    // Certify every covered chip except mid-turn, which reads failing.
    serve(payload((id, chip) => (chip === "steerMidTurn" ? "failing" : "certified")));
    const railFor = renderRails();
    for (const id of IDS) {
      const covered = GENERATED_PROVIDER_CAPABILITIES[id].proven;
      const certified: ProvenChips = { ...covered, steerMidTurn: false };
      const rail = railFor(id);
      await waitFor(() =>
        expect(chipsOf(rail, "data-supported")).toEqual(CHIP_ORDER.map((chip) => String(certified[chip]))),
      );
      if (covered.steerMidTurn) {
        expect(rail.querySelector('[data-capability="steer"]')?.getAttribute("data-certification")).toBe("failing");
      }
      const summary = rail.querySelector("p")?.textContent ?? "";
      const pendingAt = summary.search(/[^.]*not currently proven/);
      const claim = summary.slice(0, Math.max(pendingAt, 0)).toLowerCase();
      const pending = summary.slice(Math.max(pendingAt, 0)).toLowerCase();
      for (const [proven, label] of [
        [certified.launchAndSend, "launch and send"],
        [certified.interrupt, "interrupt"],
        [certified.steerMidTurn, "mid-turn steering"],
        [certified.resume, "resume"],
      ] as const) {
        expect(
          (proven ? claim : pending).includes(label),
          `${id}: "${label}" belongs in the ${proven ? "claim" : "pending"} clause — got "${summary}"`,
        ).toBe(true);
      }
    }
  });
});
