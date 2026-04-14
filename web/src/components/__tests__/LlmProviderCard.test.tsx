import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as systemApi from "../../services/api/system";
import LlmProviderCard from "../LlmProviderCard";

vi.mock("../../services/api/system", () => ({
  fetchLlmCapabilities: vi.fn(),
  fetchEffectiveLlmProviders: vi.fn(),
  upsertLlmProvider: vi.fn(),
  deleteLlmProvider: vi.fn(),
  testLlmProvider: vi.fn(),
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
      <LlmProviderCard />
    </QueryClientProvider>,
  );
}

describe("LlmProviderCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(systemApi.fetchLlmCapabilities).mockResolvedValue({
      text: {
        available: true,
        source: null,
        provider_name: null,
        features: ["chat", "summaries"],
      },
      embedding: {
        available: false,
        source: null,
        provider_name: null,
        features: ["recall"],
      },
    });
    vi.mocked(systemApi.fetchEffectiveLlmProviders).mockResolvedValue([
      {
        capability: "text",
        provider_name: "openrouter",
        base_url: "https://openrouter.ai/api/v1",
        api_key_preview: "sk-o...1234",
        source: "environment",
        has_key: true,
        created_at: null,
        updated_at: null,
      },
    ]);
    vi.mocked(systemApi.upsertLlmProvider).mockResolvedValue({ success: true });
    vi.mocked(systemApi.deleteLlmProvider).mockResolvedValue(undefined);
    vi.mocked(systemApi.testLlmProvider).mockResolvedValue({ success: true, message: "ok" });
  });

  it("shows current provider state before revealing edit controls", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click((await screen.findAllByRole("button", { name: "Configure" }))[0]);

    expect(await screen.findByDisplayValue("openrouter")).toBeTruthy();
    expect(screen.getByDisplayValue("sk-o...1234")).toBeTruthy();
    expect(await screen.findByDisplayValue("https://openrouter.ai/api/v1")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Edit Settings" }));
    expect(screen.getByRole("radio", { name: "OpenRouter (recommended)" })).toHaveProperty("checked", true);
    expect(screen.getByPlaceholderText("Replace current API key")).toBeTruthy();
  });
});
