/**
 * The browser half of the `longhouse auth` handshake.
 *
 * This page and the engine's loopback listener are two halves of one wire
 * format (`read_callback_request` / `callback_code` in engine/src/longhouse.rs):
 * a top-level GET to http://127.0.0.1:<port>/connected carrying `state` and a
 * one-time `code`. The page must never hold a device token, and approving must
 * not mint one -- the CLI redeems the code with its PKCE verifier.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../../components/confirm";
import DevicesPage, { connectServerCommand } from "../DevicesPage";

const deviceApiMocks = vi.hoisted(() => ({
  listDeviceTokens: vi.fn(),
  createDeviceToken: vi.fn(),
  createDeviceConnectCode: vi.fn(),
  revokeDeviceToken: vi.fn(),
}));

vi.mock("../../services/api/devices", () => deviceApiMocks);

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

const CALLBACK = "http://127.0.0.1:54321/connected";
const STATE = "8f1c0f6e-1c1c-4a5e-9d21-7d0d2b8a51aa";
const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
// Characters URL encoding has to escape, so an encoding change on either side
// shows up here rather than in a failed device setup.
const CODE = "Ab_c-9+z/=";

function renderDevicesPage(search: string) {
  window.history.replaceState({}, "", `/settings/devices${search}`);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ConfirmProvider>
        <DevicesPage />
      </ConfirmProvider>
    </QueryClientProvider>
  );
}

function connectSearch(extra: Record<string, string> = { challenge: CHALLENGE }) {
  const params = new URLSearchParams({
    connect: "1",
    callback: CALLBACK,
    state: STATE,
    device: "This Mac",
    ...extra,
  });
  return `?${params.toString()}`;
}

describe("DevicesPage device-auth callback", () => {
  let replace: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    deviceApiMocks.listDeviceTokens.mockResolvedValue({ tokens: [], total: 0 });
    deviceApiMocks.createDeviceConnectCode.mockResolvedValue({ code: CODE, expires_in: 300 });
    replace = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  // jsdom cannot navigate; capture where the page sends the browser.
  function captureNavigation() {
    const real = window.location;
    vi.spyOn(window, "location", "get").mockReturnValue({
      href: real.href,
      origin: real.origin,
      search: real.search,
      pathname: real.pathname,
      replace,
    } as unknown as Location);
  }

  it("approves with a code bound to the CLI's challenge and navigates to the loopback with it", async () => {
    const user = userEvent.setup();
    renderDevicesPage(connectSearch());
    captureNavigation();

    await user.click(await screen.findByRole("button", { name: /connect this device/i }));
    await waitFor(() => expect(replace).toHaveBeenCalledTimes(1));

    expect(deviceApiMocks.createDeviceConnectCode.mock.calls[0][0]).toEqual({
      device_id: "This Mac",
      code_challenge: CHALLENGE,
    });
    // Approving must not mint a token in the browser.
    expect(deviceApiMocks.createDeviceToken).not.toHaveBeenCalled();

    const target = new URL(replace.mock.calls[0][0]);
    expect(`${target.origin}${target.pathname}`).toBe(CALLBACK);
    // `callback_code` reads exactly these two keys.
    expect([...target.searchParams.keys()].sort()).toEqual(["code", "state"]);
    expect(target.searchParams.get("state")).toBe(STATE);
    expect(target.searchParams.get("code")).toBe(CODE);
    expect(target.href).not.toContain("zdt_");
  });

  it("says so when the browser never reached the CLI, instead of spinning forever", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderDevicesPage(connectSearch());
      captureNavigation();

      await user.click(await screen.findByRole("button", { name: /connect this device/i }));
      await waitFor(() => expect(replace).toHaveBeenCalledTimes(1));
      expect(screen.queryByText(/could not reach longhouse auth/i)).toBeNull();

      await vi.advanceTimersByTimeAsync(6000);
      expect(await screen.findByText(/could not reach longhouse auth on this mac/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /not connected/i })).toBeDisabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows why approval failed instead of doing nothing", async () => {
    deviceApiMocks.createDeviceConnectCode.mockRejectedValue(new Error("Too many device connections are pending"));
    const user = userEvent.setup();
    renderDevicesPage(connectSearch());
    captureNavigation();

    await user.click(await screen.findByRole("button", { name: /connect this device/i }));
    expect(await screen.findByText(/could not approve this device: too many device connections/i)).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("tells an out-of-date CLI to update instead of offering a dead button", async () => {
    renderDevicesPage(connectSearch({}));
    expect(await screen.findByText(/too old to connect through this page/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect this device/i })).toBeNull();
  });

  it("reports the outcome the engine's 303 redirect carries back", async () => {
    renderDevicesPage("?connected=1");
    expect(await screen.findByText(/device connected/i)).toBeInTheDocument();

    renderDevicesPage("?connected=0");
    expect(await screen.findByText(/device connection failed/i)).toBeInTheDocument();
  });

  it("ignores a callback that is not the local listener", async () => {
    const params = new URLSearchParams({
      connect: "1",
      callback: "https://evil.example/connected",
      state: STATE,
      device: "This Mac",
      challenge: CHALLENGE,
    });
    renderDevicesPage(`?${params.toString()}`);

    await screen.findByRole("button", { name: /create token/i });
    expect(screen.queryByRole("button", { name: /connect this device/i })).toBeNull();
  });

  it("gives a headless server one line that installs and connects it", () => {
    const line = connectServerCommand("vps-1", "zdt_abc'def");
    expect(line).toMatch(/^curl -fsSL https:\/\/get\.longhouse\.ai\/install\.sh \| /);
    expect(line).toContain("LONGHOUSE_DEVICE_TOKEN='zdt_abc'\\''def'");
    expect(line).toContain("LONGHOUSE_MACHINE_NAME='vps-1'");
    expect(line).toContain(`LONGHOUSE_URL='${window.location.origin}'`);
    expect(line.endsWith(" bash")).toBe(true);
  });
});
