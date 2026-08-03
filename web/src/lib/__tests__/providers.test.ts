import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  getLaunchProviderSupport,
  getLaunchProviderSupportList,
  getProviderLabel,
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

type DeviceEntrypoint = {
  id: string;
  status: string;
  native_target_command: string;
  providers: string[] | string;
};

type DeviceEntrypoints = {
  commands: DeviceEntrypoint[];
};

function loadDeviceEntrypoints(): DeviceEntrypoints {
  return JSON.parse(
    readFileSync(
      resolve(__dirname, "../../../../config/native_device_entrypoints.json"),
      "utf-8",
    ),
  ) as DeviceEntrypoints;
}

const contractsPath = resolve(
  process.cwd(),
  "../server/zerg/config/managed_provider_contracts.json",
);

function loadContracts(): ManagedProviderContracts {
  return JSON.parse(readFileSync(contractsPath, "utf-8")) as ManagedProviderContracts;
}

describe("providers launch support", () => {
  it("uses generated provider display and marketing names", () => {
    expect(getProviderLabel("opencode")).toBe("OpenCode");
    expect(getProviderLabel("openai")).toBe("OpenAI");
    expect(getProviderLabel("gemini")).toBe("Antigravity");
    expect(getProviderLabel("z.ai")).toBe("Z.ai");
    expect(getProviderLabel("new_provider")).toBe("New Provider");
    expect(getLaunchProviderSupport("codex")?.marketingName).toBe("Codex CLI");
  });

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
        // Antigravity dropped out: it is Shadow-only, so it can be archived but
        // not launched or sent to.
        .filter((provider) => ["claude", "codex", "cursor", "opencode"].includes(provider.id))
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
      resume: true,
      cloudSessionStart: "live",
      hooksSupport: "none",
    });
    expect(getLaunchProviderSupport("antigravity")).toMatchObject({
      // Shadow-only: send_input is policy_disabled in the contract and the
      // engine no longer advertises antigravity.send.
      launchAndSend: false,
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

    // The launch command is gated by the device entrypoint, not by the contract
    // capability flags. Antigravity supports launch_local while its entrypoint is
    // excluded, so the table must not offer a command the facade does not have.
    const entrypoints = loadDeviceEntrypoints();
    for (const id of launchIds) {
      const entry = entrypoints.commands.find(
        (command) => command.id === `${id}-managed`,
      );
      const offered = entry?.status === "available";
      const support = getLaunchProviderSupport(id)!;
      expect(
        support.nativeLaunchCommand,
        `${id}: nativeLaunchCommand must match device entrypoint availability`,
      ).toBe(offered ? entry!.native_target_command : null);
    }

    expect(contracts.providers.map((provider) => provider.provider).sort()).toEqual(
      [...launchIds].sort(),
    );
  });
});
