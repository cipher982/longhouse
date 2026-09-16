import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import {
  GENERATED_PROVIDER_CAPABILITIES,
  type GeneratedProviderId,
} from "../../../generated/provider-capabilities";
import { lookupProviderBrand } from "../../../generated/provider-brands";

// The rails render generated contract data, so the expectations are derived
// from that same contract rather than transcribed. A hand-typed matrix here
// would only detect change, not verify truth.
const capabilityRow = (id: GeneratedProviderId) => {
  const c = GENERATED_PROVIDER_CAPABILITIES[id].proven;
  return [
    c.search,
    c.launchAndSend,
    c.interrupt,
    c.steerMidTurn,
    c.resume,
  ].map(String);
};

describe("landing provider claims", () => {
  it("renders every provider rail straight from the capability contract", () => {
    render(
      <MemoryRouter>
        <IntegrationsSection />
      </MemoryRouter>,
    );

    const searchable = Object.values(GENERATED_PROVIDER_CAPABILITIES).some(
      (c) => c.proven.search,
    );
    expect(
      screen.queryByText("Sync, timeline, and full-text search") !== null,
      "the universal search claim must follow the search proof edge",
    ).toBe(searchable);

    const rails = screen.getAllByRole("listitem");
    const railFor = (name: string) =>
      rails.find(
        (item) =>
          item.querySelector(".landing-provider-row-name")?.textContent ===
          name,
      );

    for (const id of Object.keys(
      GENERATED_PROVIDER_CAPABILITIES,
    ) as GeneratedProviderId[]) {
      const name = lookupProviderBrand(id).marketingName;
      const rail = railFor(name);
      expect(rail, `no landing rail for ${id} (${name})`).toBeDefined();
      const chips = Array.from(
        rail!.querySelectorAll(".landing-provider-capability"),
      ).map((chip) => chip.getAttribute("data-supported"));
      expect(
        chips,
        `${name} chips disagree with the provider contract`,
      ).toEqual(capabilityRow(id));
    }

    // Counting one phrase across the page cannot catch a row that claims a
    // capability it does not have. Check each summary against its own contract
    // row instead: a proven capability is named in the opening claim, an
    // unproven one only in the "not yet release-proven" clause.
    for (const id of Object.keys(
      GENERATED_PROVIDER_CAPABILITIES,
    ) as GeneratedProviderId[]) {
      const c = GENERATED_PROVIDER_CAPABILITIES[id].proven;
      const name = lookupProviderBrand(id).marketingName;
      const summary = railFor(name)?.querySelector("p")?.textContent ?? "";
      expect(summary, `${name} has no summary`).not.toBe("");
      if (c.launchAndSend && c.interrupt && c.steerMidTurn && c.resume) {
        expect(summary).toBe("Full remote control, including steering during a turn.");
        continue;
      }
      const pendingAt = summary.search(/[^.]*not yet release-proven/);
      const claim = summary.slice(0, Math.max(pendingAt, 0)).toLowerCase();
      const pending = summary.slice(Math.max(pendingAt, 0)).toLowerCase();
      for (const [proven, label] of [
        [c.launchAndSend, "launch and send"],
        [c.interrupt, "interrupt"],
        [c.steerMidTurn, "mid-turn steering"],
        [c.resume, "resume"],
      ] as const) {
        expect(
          (proven ? claim : pending).includes(label),
          `${name}: "${label}" belongs in the ${proven ? "claim" : "pending"} clause — got "${summary}"`,
        ).toBe(true);
      }
    }
  });
});
