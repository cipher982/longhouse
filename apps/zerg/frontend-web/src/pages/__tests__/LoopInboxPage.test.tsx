import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LoopInboxPage from "../LoopInboxPage";
import {
  applyLoopInboxAction,
  fetchLoopActionCard,
  fetchLoopActionCardForSession,
  fetchLoopInbox,
  type LoopActionCard,
  type LoopInboxItem,
} from "../../services/api/oikos";
import { useLoopInstallPrompt } from "../../hooks/useLoopInstallPrompt";
import { TestRouter } from "../../test/test-utils";

const mockNavigate = vi.fn();
let mockSessionId: string | undefined;
let mockCardId = "42";

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useParams: () => ({ sessionId: mockSessionId, cardId: mockCardId }),
  };
});

vi.mock("../../services/api/oikos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api/oikos")>();
  return {
    ...actual,
    fetchLoopInbox: vi.fn(),
    fetchLoopActionCard: vi.fn(),
    fetchLoopActionCardForSession: vi.fn(),
    applyLoopInboxAction: vi.fn(),
  };
});

vi.mock("../../hooks/useLoopInstallPrompt", () => ({
  useLoopInstallPrompt: vi.fn(),
}));

const fetchLoopInboxMock = vi.mocked(fetchLoopInbox);
const fetchLoopActionCardMock = vi.mocked(fetchLoopActionCard);
const fetchLoopActionCardForSessionMock = vi.mocked(fetchLoopActionCardForSession);
const applyLoopInboxActionMock = vi.mocked(applyLoopInboxAction);
const useLoopInstallPromptMock = vi.mocked(useLoopInstallPrompt);

function makeInboxItem(overrides: Partial<LoopInboxItem> = {}): LoopInboxItem {
  return {
    cardId: 42,
    sessionId: "sess-1",
    title: "Session Detail Page",
    project: "zerg",
    machine: "cinder",
    provider: "claude",
    loopMode: "assist",
    decision: "continue",
    executionState: "awaiting_user_approval",
    summary: "Only targeted verification remains.",
    recommendedAction: "continue_session",
    followUpPrompt: "Run the pending targeted tests.",
    blockedReasons: [],
    lastTurnAt: "2026-03-19T12:00:00Z",
    cardState: "active",
    cardStateReason: null,
    supersededByCardId: null,
    requiresAttention: true,
    ...overrides,
  };
}

function makeActionCard(overrides: Partial<LoopActionCard> = {}): LoopActionCard {
  return {
    ...makeInboxItem(),
    rationale: "This is the routine continue case.",
    modeCapability: "notify_only",
    modeSummary: "Suggest or escalate from completed turns, but wait for approval before continuing.",
    lastUserText: "finish the session detail page",
    lastAssistantText: "Only targeted verification remains. Run the pending targeted tests.",
    availableActions: ["approve_recommended_action", "not_now"],
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={["/loop/card/42"]}>
        <LoopInboxPage />
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("LoopInboxPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSessionId = undefined;
    mockCardId = "42";
    fetchLoopInboxMock.mockResolvedValue([makeInboxItem()]);
    fetchLoopActionCardMock.mockResolvedValue(makeActionCard());
    fetchLoopActionCardForSessionMock.mockResolvedValue(makeActionCard());
    applyLoopInboxActionMock.mockResolvedValue({
      sessionId: "sess-1",
      reviewId: 42,
      action: "approve_recommended_action",
      status: "acted",
      reason: "continue_session",
      queuedJobId: 7,
    });
    useLoopInstallPromptMock.mockReturnValue({
      canInstall: false,
      showIosHint: false,
      isInstalled: false,
      install: vi.fn(),
    });
  });

  it("renders the inbox row and action card", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-inbox-row-42")).toBeInTheDocument();
    });

    const card = screen.getByTestId("loop-inbox-card");
    expect(screen.getAllByText("Session Detail Page")).toHaveLength(2);
    expect(within(card).getByText(/^Only targeted verification remains\.$/i)).toBeInTheDocument();
    expect(within(card).getByText(/^Run the pending targeted tests\.$/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open full session/i })).toHaveAttribute("href", "/timeline/sess-1");
  });

  it("sends the approve action for the selected session", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-approve-action")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("loop-approve-action"));

    await waitFor(() => {
      expect(applyLoopInboxActionMock).toHaveBeenCalledWith(42, "approve_recommended_action");
    });
  });

  it("renders a stale card even when the inbox is empty", async () => {
    fetchLoopInboxMock.mockResolvedValue([]);
    fetchLoopActionCardMock.mockResolvedValue(
      makeActionCard({
        cardState: "superseded",
        cardStateReason: "A newer turn replaced this follow-up.",
        supersededByCardId: 99,
        availableActions: [],
      }),
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Superseded")).toBeInTheDocument();
    });

    expect(screen.getByText("A newer turn replaced this follow-up.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open latest/i })).toHaveAttribute("href", "/loop/card/99");
  });

  it("shows the install banner when loop can be installed", async () => {
    const user = userEvent.setup();
    const installMock = vi.fn().mockResolvedValue(true);
    useLoopInstallPromptMock.mockReturnValue({
      canInstall: true,
      showIosHint: false,
      isInstalled: false,
      install: installMock,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-install-banner")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("loop-install-action"));

    expect(installMock).toHaveBeenCalledTimes(1);
  });
});
