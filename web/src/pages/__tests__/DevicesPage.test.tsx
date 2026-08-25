/**
 * The browser half of the `longhouse auth --browser` handshake.
 *
 * This page and the engine's callback listener are two halves of one wire
 * format, and shipping only one half has already broken device auth twice.
 * The assertions below pin the exact shape the listener parses in
 * `read_callback_request` / `callback_token` (engine/src/longhouse.rs):
 * method POST, `application/x-www-form-urlencoded`, and the two field names
 * `state` and `token`. Change either side and this test fails first.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../../components/confirm";
import DevicesPage from "../DevicesPage";

const deviceApiMocks = vi.hoisted(() => ({
  listDeviceTokens: vi.fn(),
  createDeviceToken: vi.fn(),
  revokeDeviceToken: vi.fn(),
}));

vi.mock("../../services/api/devices", () => deviceApiMocks);

vi.mock("../../lib/readiness-contract", () => ({
  useReadinessFlag: vi.fn(),
}));

const CALLBACK = "http://127.0.0.1:54321/connected";
const STATE = "8f1c0f6e-1c1c-4a5e-9d21-7d0d2b8a51aa";
// A urlsafe-base64 body like `secrets.token_urlsafe` produces, plus the
// characters form encoding actually has to escape, so an encoding change on
// either side shows up here rather than in a failed device setup.
const TOKEN = "zdt_A+B/C=D E_F-Gxy9";

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

function connectSearch() {
  const params = new URLSearchParams({
    connect: "1",
    callback: CALLBACK,
    state: STATE,
    device: "This Mac",
  });
  return `?${params.toString()}`;
}

describe("DevicesPage device-auth callback", () => {
  let submitted: HTMLFormElement | null = null;
  let submitSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    submitted = null;
    deviceApiMocks.listDeviceTokens.mockResolvedValue({ tokens: [], total: 0 });
    deviceApiMocks.createDeviceToken.mockResolvedValue({
      id: "tok-1",
      device_id: "This Mac",
      token: TOKEN,
      created_at: "2026-08-25T00:00:00Z",
    });
    // jsdom does not implement form submission; capture the form the page
    // actually built and appended, at the moment it tries to submit it.
    submitSpy = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {
      submitted = document.querySelector<HTMLFormElement>("form[hidden]");
    });
  });

  afterEach(() => {
    submitSpy.mockRestore();
    window.history.replaceState({}, "", "/");
  });

  it("hands the token to the CLI as a form POST the engine listener can parse", async () => {
    const user = userEvent.setup();
    renderDevicesPage(connectSearch());

    await user.click(await screen.findByRole("button", { name: /connect this device/i }));
    await waitFor(() => expect(submitted).not.toBeNull());

    const form = submitted!;
    // The listener rejects anything that is not a POST, and reads the body by
    // Content-Length -- a GET or a multipart body reaches it as no params.
    expect(form.method.toUpperCase()).toBe("POST");
    expect(form.enctype).toBe("application/x-www-form-urlencoded");
    expect(form.action).toBe(CALLBACK);

    const fields = Object.fromEntries(
      [...form.querySelectorAll("input")].map((input) => [input.name, input.value])
    );
    // `callback_token` reads exactly these two keys.
    expect(Object.keys(fields).sort()).toEqual(["state", "token"]);
    expect(fields.state).toBe(STATE);
    expect(fields.token).toBe(TOKEN);
    // Whatever the browser escapes on the wire, the decoded value the engine
    // stores has to be the token byte-for-byte.
    expect(new URLSearchParams(new FormData(form) as never).get("token")).toBe(TOKEN);

    expect(deviceApiMocks.createDeviceToken.mock.calls[0][0]).toEqual({ device_id: "This Mac" });
  });

  it("never puts the token in a URL", async () => {
    const user = userEvent.setup();
    renderDevicesPage(connectSearch());

    await user.click(await screen.findByRole("button", { name: /connect this device/i }));
    await waitFor(() => expect(submitted).not.toBeNull());

    // A device token does not expire, so a query-string callback would write a
    // live credential into browser history permanently. Neither the form target
    // nor the page's own URL may carry it.
    expect(submitted!.action).not.toContain("zdt_");
    expect(window.location.href).not.toContain("zdt_");
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
    });
    renderDevicesPage(`?${params.toString()}`);

    await screen.findByRole("button", { name: /create token/i });
    expect(screen.queryByRole("button", { name: /connect this device/i })).toBeNull();
  });
});
