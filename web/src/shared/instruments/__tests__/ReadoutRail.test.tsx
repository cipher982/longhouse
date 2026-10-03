import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReadoutRail, type ReadoutRailProps } from "../ReadoutRail";
import { listMachines, type MachineDirectoryEntry } from "@/shared/api/index";

vi.mock("@/shared/api/index", () => ({
  listMachines: vi.fn(),
}));

const listMachinesMock = vi.mocked(listMachines);

function machine(deviceId: string, online: boolean): MachineDirectoryEntry {
  return {
    device_id: deviceId,
    machine_name: deviceId,
    online,
    control_channel_status: online ? "connected" : "disconnected",
    launch: { providers: [], blocked_by: online ? "no_launch_support" : "control_down" },
  } as MachineDirectoryEntry;
}

function renderRail(props: Partial<ReadoutRailProps> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const defaults: ReadoutRailProps = {
    turnSeconds: null,
    turnLive: false,
    contextTokens: null,
    contextWindow: null,
    toolCallsThisTurn: null,
    toolCallsLive: false,
    waitingOn: null,
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <ReadoutRail {...defaults} {...props} />
    </QueryClientProvider>,
  );
}

describe("ReadoutRail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listMachinesMock.mockResolvedValue({ machines: [] });
  });

  it("renders only the entries it has real data for", () => {
    renderRail({
      turnSeconds: 2137,
      turnLive: true,
      toolCallsThisTurn: 18,
      toolCallsLive: true,
    });

    expect(screen.getByTestId("readout-turn")).toBeInTheDocument();
    expect(screen.getByTestId("readout-tool-calls")).toBeInTheDocument();
    expect(screen.queryByTestId("readout-context")).not.toBeInTheDocument();
    expect(screen.queryByTestId("readout-waiting-on")).not.toBeInTheDocument();
  });

  it("omits Context when only one of tokens/window is known", () => {
    renderRail({ contextTokens: 25_210, contextWindow: null });
    expect(screen.queryByTestId("readout-context")).not.toBeInTheDocument();
  });

  it("shows Context with a fill bar when both fields are known", () => {
    renderRail({ contextTokens: 25_210, contextWindow: 258_400 });
    const readout = screen.getByTestId("readout-context");
    expect(readout).toHaveTextContent("25k / 258k");
  });

  it("dims Turn and Tool calls when not running", () => {
    renderRail({ turnSeconds: 58, turnLive: false, toolCallsThisTurn: 3, toolCallsLive: false });
    expect(screen.getByTestId("readout-turn").querySelector(".instrument-nixie--dim")).toBeTruthy();
    expect(screen.getByTestId("readout-tool-calls").querySelector(".instrument-nixie--dim")).toBeTruthy();
  });

  it("shows Waiting on only when a tool is running", () => {
    renderRail({ waitingOn: { toolName: "hub", label: "bg_3 backend suite" } });
    const readout = screen.getByTestId("readout-waiting-on");
    expect(readout).toHaveTextContent("hub");
    expect(readout).toHaveTextContent("bg_3 backend suite");
  });

  it("keeps the capsule to the bare tool name even for a long label", () => {
    const longLabel =
      "Recapturing every session-detail viewport now that the density and trace-line changes are in place";
    renderRail({ waitingOn: { toolName: "hub", label: longLabel } });
    const readout = screen.getByTestId("readout-waiting-on");
    expect(readout.querySelector(".instrument-nixie")).toHaveTextContent("hub");
    const labelEl = readout.querySelector(".instrument-readout__waiting-label");
    expect(labelEl).toHaveTextContent(longLabel);
    expect(labelEl).toHaveAttribute("title", longLabel);
  });

  it("omits the machine count when no machine is enrolled", async () => {
    renderRail({});
    await waitFor(() => expect(listMachinesMock).toHaveBeenCalled());
    expect(screen.queryByTestId("readout-machines")).not.toBeInTheDocument();
  });

  it("counts connected Machine Agents, not Runners", async () => {
    listMachinesMock.mockResolvedValue({
      machines: [machine("cinder", true), machine("cube", true), machine("cube-canary", false), machine("pepper", false)],
    });
    renderRail({});
    await waitFor(() => expect(screen.getByTestId("readout-machines")).toBeInTheDocument());
    expect(screen.getByTestId("readout-machines")).toHaveTextContent("2 / 4");
    expect(screen.getByTestId("readout-machines").querySelector(".instrument-nixie--dim")).toBeTruthy();
  });
});
