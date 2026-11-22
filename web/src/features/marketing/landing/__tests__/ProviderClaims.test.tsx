import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import { GENERATED_PROVIDER_CAPABILITIES, type GeneratedProviderId } from "@/generated/provider-capabilities";
import { lookupProviderBrand } from "@/generated/provider-brands";

// The rails render the generated contract data, so expectations are derived
// from that same contract rather than transcribed. A hand-typed provider table
// here would only detect change.
const IDS = Object.keys(GENERATED_PROVIDER_CAPABILITIES) as GeneratedProviderId[];
const CHIP_ORDER = ["search", "launchAndSend", "interrupt", "steerMidTurn", "resume"] as const;

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

describe("landing provider claims", () => {
  it("lights each chip from the generated contract", () => {
    const railFor = renderRails();
    for (const id of IDS) {
      const chips = Array.from(railFor(id).querySelectorAll(".landing-provider-capability")).map((chip) =>
        chip.getAttribute("data-supported"),
      );
      expect(chips).toEqual(CHIP_ORDER.map((chip) => String(GENERATED_PROVIDER_CAPABILITIES[id][chip])));
    }
  });

  it("makes no claim about proof or certification", () => {
    renderRails();
    expect(screen.queryAllByText(/certif|proven|proof/i)).toHaveLength(0);
  });

  it("summaries name each supported and unsupported capability in the right clause", () => {
    const railFor = renderRails();
    for (const id of IDS) {
      const support = GENERATED_PROVIDER_CAPABILITIES[id];
      const summary = railFor(id).querySelector("p")?.textContent ?? "";
      if (support.launchAndSend && support.interrupt && support.steerMidTurn && support.resume) {
        expect(summary).toBe("Full remote control, including steering during a turn.");
        continue;
      }
      const missingAt = summary.search(/[^.]*not supported/);
      const claim = summary.slice(0, Math.max(missingAt, 0)).toLowerCase();
      const missing = summary.slice(Math.max(missingAt, 0)).toLowerCase();
      for (const [supported, label] of [
        [support.launchAndSend, "launch and send"],
        [support.interrupt, "interrupt"],
        [support.steerMidTurn, "mid-turn steering"],
        [support.resume, "resume"],
      ] as const) {
        expect(
          (supported ? claim : missing).includes(label),
          `${id}: "${label}" belongs in the ${supported ? "claim" : "unsupported"} clause - got "${summary}"`,
        ).toBe(true);
      }
    }
  });
});
