import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReadoutRail, type ReadoutRailProps } from "../ReadoutRail";
import { fetchRunnerStatus } from "../../../services/api";

vi.mock("../../../services/api", () => ({
  fetchRunnerStatus: vi.fn(),
}));

const fetchRunnerStatusMock = vi.mocked(fetchRunnerStatus);

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
    waitingOnLabel: null,
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
    fetchRunnerStatusMock.mockResolvedValue({
      total: 0,
      online: 0,
      offline: 0,
      runners: [],
    });
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
    renderRail({ waitingOnLabel: "bg_3 backend suite" });
    expect(screen.getByTestId("readout-waiting-on")).toHaveTextContent("bg_3 backend suite");
  });

  it("omits Machines up when the runner status is unavailable", async () => {
    renderRail({});
    await waitFor(() => expect(fetchRunnerStatusMock).toHaveBeenCalled());
    expect(screen.queryByTestId("readout-machines")).not.toBeInTheDocument();
  });

  it("shows Machines up, dim, once the runner status resolves", async () => {
    fetchRunnerStatusMock.mockResolvedValue({
      total: 12,
      online: 4,
      offline: 8,
      runners: [],
    });
    renderRail({});
    await waitFor(() => expect(screen.getByTestId("readout-machines")).toBeInTheDocument());
    expect(screen.getByTestId("readout-machines")).toHaveTextContent("4 / 12");
    expect(screen.getByTestId("readout-machines").querySelector(".instrument-nixie--dim")).toBeTruthy();
  });
});
