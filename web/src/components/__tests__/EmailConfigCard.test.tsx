import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as emailApi from "../../services/api/emailConfig";
import EmailConfigCard from "../EmailConfigCard";

vi.mock("../../services/api/emailConfig", () => ({
  fetchEmailStatus: vi.fn(),
  saveEmailConfig: vi.fn(),
  testEmail: vi.fn(),
  deleteEmailConfig: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <EmailConfigCard />
    </QueryClientProvider>,
  );
}

describe("EmailConfigCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(emailApi.fetchEmailStatus).mockResolvedValue({
      configured: true,
      source: "env",
      aws_ses_access_key_preview: "AKIA...1234",
      aws_ses_secret_access_key_preview: "secr...abcd",
      aws_ses_region: "eu-west-1",
      from_email: "notify@longhouse.ai",
      notify_email: "owner@example.com",
      keys: [
        { key: "AWS_SES_ACCESS_KEY_ID", configured: true, source: "env" },
        { key: "AWS_SES_SECRET_ACCESS_KEY", configured: true, source: "env" },
        { key: "AWS_SES_REGION", configured: true, source: "env" },
        { key: "FROM_EMAIL", configured: true, source: "env" },
        { key: "NOTIFY_EMAIL", configured: true, source: "env" },
      ],
    });
    vi.mocked(emailApi.saveEmailConfig).mockResolvedValue({ success: true, keys_saved: 1 });
    vi.mocked(emailApi.testEmail).mockResolvedValue({ success: true, message: "sent" });
    vi.mocked(emailApi.deleteEmailConfig).mockResolvedValue({ success: true, keys_deleted: 1 });
  });

  it("shows read-only current config previews before revealing edit inputs", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(await screen.findByRole("button", { name: "Configure" }));

    expect(await screen.findByDisplayValue("AKIA...1234")).toBeTruthy();
    expect(screen.getByDisplayValue("secr...abcd")).toBeTruthy();
    expect(await screen.findByDisplayValue("eu-west-1")).toBeTruthy();
    expect(screen.getByDisplayValue("notify@longhouse.ai")).toBeTruthy();
    expect(screen.getByDisplayValue("owner@example.com")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Edit Settings" }));
    expect(screen.getByPlaceholderText("Replace current access key")).toBeTruthy();
    expect(screen.getByPlaceholderText("Replace current secret key")).toBeTruthy();
  });
});
