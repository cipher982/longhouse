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
  const c = GENERATED_PROVIDER_CAPABILITIES[id];
  return [
    c.launchAndSend,
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

    expect(
      screen.getByText("Sync, timeline, and full-text search"),
    ).toBeInTheDocument();

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
    // row instead: a supported capability is named in the opening claim, an
    // unsupported one only after it, in the "not available yet" clause.
    for (const id of Object.keys(
      GENERATED_PROVIDER_CAPABILITIES,
    ) as GeneratedProviderId[]) {
      const c = GENERATED_PROVIDER_CAPABILITIES[id];
      if (c.steerMidTurn) continue;
      const name = lookupProviderBrand(id).marketingName;
      const summary = railFor(name)?.querySelector("p")?.textContent ?? "";
      expect(summary, `${name} has no summary`).not.toBe("");
      const [claim, denial = ""] = summary.split(/\.\s+/, 2);
      for (const capability of ["interrupt", "resume"] as const) {
        const supported = c[capability];
        expect(
          claim.toLowerCase().includes(capability),
          `${name}: summary ${supported ? "must" : "must not"} claim ${capability} — got "${summary}"`,
        ).toBe(supported);
        if (!supported && c.launchAndSend) {
          expect(
            denial.toLowerCase(),
            `${name} must say ${capability} is unavailable`,
          ).toContain(capability);
        }
      }
    }
  });
});
