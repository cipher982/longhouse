import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  getLaunchProviderSupport,
  getLaunchProviderSupportList,
  type LaunchProviderId,
} from "../providers";

type ContractProvider = {
  provider: string;
  launch_local: boolean;
  send_input: boolean;
  interrupt: boolean;
  terminate: boolean;
  steer_active_turn: boolean;
  can_resume: boolean;
  turn_start: boolean;
};

type ManagedProviderContracts = {
  providers: ContractProvider[];
};

const contractsPath = resolve(
  process.cwd(),
  "../server/zerg/config/managed_provider_contracts.json",
);

function loadContracts(): ManagedProviderContracts {
  return JSON.parse(readFileSync(contractsPath, "utf-8")) as ManagedProviderContracts;
}

describe("providers launch support", () => {
  it("keeps the launch provider capability contract explicit", () => {
    const providers = getLaunchProviderSupportList();
    expect(providers.map((provider) => provider.id)).toEqual([
      "claude",
      "codex",
      "cursor",
      "opencode",
      "antigravity",
    ]);
    expect(providers.every((provider) => provider.archiveVisibility === "live")).toBe(true);
    expect(getLaunchProviderSupport("antigravity")?.cloudSessionStart).toBe("none");
    expect(
      providers
        .filter((provider) => ["claude", "codex", "cursor", "opencode"].includes(provider.id))
        .every((provider) => provider.cloudSessionStart === "live"),
    ).toBe(true);
    expect(
      providers
        .filter((provider) => ["claude", "codex", "cursor", "opencode", "antigravity"].includes(provider.id))
        .every((provider) => provider.launchAndSend),
    ).toBe(true);
  });

  it("mirrors the managed provider contract capability matrix", () => {
    expect(getLaunchProviderSupport("claude")).toMatchObject({
      launchAndSend: true,
      interrupt: true,
      steerMidTurn: true,
      resume: true,
      cloudSessionStart: "live",
      hooksSupport: "live",
    });
    expect(getLaunchProviderSupport("codex")).toMatchObject({
      launchAndSend: true,
      interrupt: true,
      steerMidTurn: true,
      resume: true,
      cloudSessionStart: "live",
      hooksSupport: "none",
    });
    expect(getLaunchProviderSupport("cursor")).toMatchObject({
      launchAndSend: true,
      interrupt: true,
      steerMidTurn: false,
      resume: true,
      cloudSessionStart: "live",
      hooksSupport: "live",
    });
    expect(getLaunchProviderSupport("opencode")).toMatchObject({
      launchAndSend: true,
      interrupt: true,
      steerMidTurn: false,
      resume: false,
      cloudSessionStart: "live",
      hooksSupport: "none",
    });
    expect(getLaunchProviderSupport("antigravity")).toMatchObject({
      launchAndSend: true,
      interrupt: false,
      steerMidTurn: false,
      resume: false,
      cloudSessionStart: "none",
      hooksSupport: "none",
    });
  });

  it("records hooks and telemetry differences for the landing truth pass", () => {
    expect(getLaunchProviderSupport("claude")).toMatchObject({
      hooksSupport: "live",
      telemetryQuality: "rich",
    });
    expect(getLaunchProviderSupport("cursor")).toMatchObject({
      hooksSupport: "live",
      telemetryQuality: "structured",
    });
    expect(getLaunchProviderSupport("codex")).toMatchObject({
      hooksSupport: "none",
      telemetryQuality: "structured",
    });
    expect(getLaunchProviderSupport("antigravity")).toMatchObject({
      hooksSupport: "none",
      telemetryQuality: "structured",
    });
  });

  it("stays in sync with managed_provider_contracts.json folding rules", () => {
    const contracts = loadContracts();
    const launchIds = new Set(getLaunchProviderSupportList().map((provider) => provider.id));

    for (const contract of contracts.providers) {
      const id = contract.provider as LaunchProviderId;
      expect(launchIds.has(id), `missing launch table entry for ${contract.provider}`).toBe(true);

      const support = getLaunchProviderSupport(id);
      expect(support).not.toBeNull();
      // launchAndSend folds launch_local + send_input; interrupt folds interrupt + terminate.
      expect(support!.launchAndSend).toBe(contract.launch_local && contract.send_input);
      expect(support!.interrupt).toBe(contract.interrupt && contract.terminate);
      expect(support!.steerMidTurn).toBe(contract.steer_active_turn);
      expect(support!.resume).toBe(contract.can_resume);
      expect(support!.cloudSessionStart).toBe(contract.turn_start ? "live" : "none");
    }

    expect(contracts.providers.map((provider) => provider.provider).sort()).toEqual(
      [...launchIds].sort(),
    );
  });
});
