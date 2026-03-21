import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes, useLocation } from "react-router-dom";
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
import { useLoopPushNotifications } from "../../hooks/useLoopPushNotifications";
import { TestRouter } from "../../test/test-utils";

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

vi.mock("../../hooks/useLoopPushNotifications", () => ({
  useLoopPushNotifications: vi.fn(),
}));

const DEFAULT_VIEWPORT_WIDTH = 1280;

const fetchLoopInboxMock = vi.mocked(fetchLoopInbox);
const fetchLoopActionCardMock = vi.mocked(fetchLoopActionCard);
const fetchLoopActionCardForSessionMock = vi.mocked(fetchLoopActionCardForSession);
const applyLoopInboxActionMock = vi.mocked(applyLoopInboxAction);
const useLoopInstallPromptMock = vi.mocked(useLoopInstallPrompt);
const useLoopPushNotificationsMock = vi.mocked(useLoopPushNotifications);

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="loop-location">{location.pathname}</div>;
}

function LoopInboxRoute() {
  return (
    <>
      <LoopInboxPage />
      <LocationProbe />
    </>
  );
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

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

function renderPage(initialEntry = "/loop/card/42") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/loop" element={<LoopInboxRoute />} />
          <Route path="/loop/card/:cardId" element={<LoopInboxRoute />} />
          <Route path="/loop/:sessionId" element={<LoopInboxRoute />} />
          <Route path="/timeline" element={<LocationProbe />} />
          <Route path="/timeline/:sessionId" element={<LocationProbe />} />
        </Routes>
      </TestRouter>
    </QueryClientProvider>,
  );
}

describe("LoopInboxPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setViewportWidth(DEFAULT_VIEWPORT_WIDTH);
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
    useLoopPushNotificationsMock.mockReturnValue({
      supported: false,
      enabledInBackend: false,
      permission: "default",
      isEnabled: false,
      isBusy: false,
      error: null,
      canEnable: false,
      canDisable: false,
      enable: vi.fn(),
      disable: vi.fn(),
    });
  });

  it("redirects the base inbox route to the first card", async () => {
    renderPage("/loop");

    await waitFor(() => {
      expect(screen.getByTestId("loop-location")).toHaveTextContent("/loop/card/42");
    });

    await waitFor(() => {
      expect(fetchLoopActionCardMock).toHaveBeenCalledWith(42);
    });
  });

  it("canonicalizes legacy session routes to the matching card", async () => {
    renderPage("/loop/sess-1");

    await waitFor(() => {
      expect(fetchLoopActionCardForSessionMock).toHaveBeenCalledWith("sess-1");
      expect(screen.getByTestId("loop-location")).toHaveTextContent("/loop/card/42");
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
    expect(screen.getByRole("link", { name: /Open current/i })).toHaveAttribute("href", "/loop/card/99");
  });

  it("hides the mobile queue toggle when only one active follow-up exists", async () => {
    setViewportWidth(390);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-inbox-card")).toBeInTheDocument();
    });

    expect(screen.getByTestId("loop-mobile-header")).toBeInTheDocument();
    expect(screen.queryByTestId("loop-mobile-queue-button")).not.toBeInTheDocument();
  });

  it("opens the mobile queue drawer and selecting another follow-up swaps the card", async () => {
    const user = userEvent.setup();
    setViewportWidth(390);

    fetchLoopInboxMock.mockResolvedValue([
      makeInboxItem(),
      makeInboxItem({
        cardId: 99,
        sessionId: "sess-2",
        title: "Runner rollout",
        summary: "The runner image is waiting for approval.",
        followUpPrompt: "Approve the runner rollout and resume the deploy.",
        lastTurnAt: "2026-03-19T12:05:00Z",
      }),
    ]);
    fetchLoopActionCardMock.mockImplementation(async (requestedCardId) => {
      if (requestedCardId === 99) {
        return makeActionCard({
          cardId: 99,
          sessionId: "sess-2",
          title: "Runner rollout",
          summary: "The runner image is waiting for approval.",
          followUpPrompt: "Approve the runner rollout and resume the deploy.",
          lastAssistantText: "The runner image build passed. Approval is the remaining step.",
        });
      }
      return makeActionCard();
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-mobile-header")).toBeInTheDocument();
      expect(screen.getByTestId("loop-mobile-queue-button")).toBeInTheDocument();
    });

    expect(screen.queryByText(/^Attention queue$/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("loop-mobile-queue-button")).toHaveAttribute(
      "aria-label",
      expect.stringMatching(/Open follow-ups/i),
    );
    expect(screen.getByTestId("loop-mobile-queue-count")).toHaveTextContent("2");
    expect(screen.getByRole("button", { name: /Open follow-ups/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open timeline/i })).not.toBeInTheDocument();

    await user.click(screen.getByTestId("loop-mobile-queue-button"));

    await waitFor(() => {
      expect(screen.getByTestId("loop-mobile-queue-drawer")).toBeInTheDocument();
      expect(screen.getByTestId("loop-mobile-queue-scrim")).toBeInTheDocument();
      expect(screen.getByTestId("loop-inbox-card")).toBeInTheDocument();
    });

    const drawer = screen.getByTestId("loop-mobile-queue-drawer");
    expect(within(drawer).getByRole("heading", { name: "Follow-ups" })).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: /Open timeline/i })).toHaveAttribute("href", "/timeline");

    await user.click(screen.getByTestId("loop-mobile-queue-close"));

    await waitFor(() => {
      expect(screen.queryByTestId("loop-mobile-queue-drawer")).not.toBeInTheDocument();
    });

    await user.click(screen.getByTestId("loop-mobile-queue-button"));
    await user.click(screen.getByTestId("loop-inbox-row-99"));

    await waitFor(() => {
      expect(screen.getByTestId("loop-location")).toHaveTextContent("/loop/card/99");
      expect(screen.queryByTestId("loop-mobile-queue-drawer")).not.toBeInTheDocument();
      expect(screen.getByTestId("loop-mobile-queue-button")).toBeInTheDocument();
    });

    const card = screen.getByTestId("loop-inbox-card");
    expect(within(card).getByRole("heading", { name: "Runner rollout" })).toBeInTheDocument();
    expect(within(card).getByText(/^Approve the runner rollout and resume the deploy\.$/i)).toBeInTheDocument();
  });

  it("auto-opens the mobile queue and flags older cards when the selected card is stale", async () => {
    setViewportWidth(390);

    fetchLoopInboxMock.mockResolvedValue([
      makeInboxItem(),
      makeInboxItem({
        cardId: 99,
        sessionId: "sess-2",
        title: "Latest follow-up",
        summary: "A newer turn replaced the older card.",
        followUpPrompt: "Continue with the latest follow-up instead.",
        lastTurnAt: "2026-03-19T12:05:00Z",
      }),
    ]);
    fetchLoopActionCardMock.mockResolvedValue(
      makeActionCard({
        cardId: 390,
        sessionId: "sess-stale",
        title: "Frontend Effect Cleanup Fully Completed",
        summary: "This older card is no longer the active thing to review.",
        cardState: "superseded",
        cardStateReason: "A newer turn replaced this follow-up.",
        supersededByCardId: 99,
        availableActions: [],
      }),
    );

    renderPage("/loop/card/390");

    await waitFor(() => {
      expect(screen.getByTestId("loop-mobile-queue-drawer")).toBeInTheDocument();
    });

    expect(screen.getByTestId("loop-mobile-header")).toBeInTheDocument();
    expect(screen.getByTestId("loop-mobile-queue-button")).toBeInTheDocument();
    expect(screen.getByTestId("loop-mobile-queue-count")).toHaveTextContent("2");

    const statusBanner = screen.getByTestId("loop-inbox-card-status-banner");
    expect(within(statusBanner).getByRole("heading", { name: /Viewing older card/i })).toBeInTheDocument();
    expect(within(statusBanner).getByText("A newer turn replaced this follow-up.")).toBeInTheDocument();
    expect(within(statusBanner).getByRole("link", { name: /Open current/i })).toHaveAttribute("href", "/loop/card/99");
  });

  it("uses compact mobile chrome and keeps the push CTA below the card", async () => {
    setViewportWidth(390);
    const enableMock = vi.fn().mockResolvedValue(true);
    useLoopPushNotificationsMock.mockReturnValue({
      supported: true,
      enabledInBackend: true,
      permission: "default",
      isEnabled: false,
      isBusy: false,
      error: null,
      canEnable: true,
      canDisable: false,
      enable: enableMock,
      disable: vi.fn(),
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-inbox-card")).toBeInTheDocument();
      expect(screen.getByTestId("loop-push-banner")).toBeInTheDocument();
    });

    expect(screen.getByTestId("loop-mobile-header")).toBeInTheDocument();
    expect(screen.queryByText(/Handle finished coding turns without opening the full desktop workspace\./i)).not.toBeInTheDocument();

    const card = screen.getByTestId("loop-inbox-card");
    const pushBanner = screen.getByTestId("loop-push-banner");
    expect(card.compareDocumentPosition(pushBanner) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("keeps the install CTA below the card on mobile selected routes", async () => {
    setViewportWidth(390);
    const installMock = vi.fn().mockResolvedValue(true);
    useLoopInstallPromptMock.mockReturnValue({
      canInstall: true,
      showIosHint: false,
      isInstalled: false,
      install: installMock,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-inbox-card")).toBeInTheDocument();
      expect(screen.getByTestId("loop-install-banner")).toBeInTheDocument();
    });

    expect(screen.getByTestId("loop-mobile-header")).toBeInTheDocument();
    const card = screen.getByTestId("loop-inbox-card");
    const installBanner = screen.getByTestId("loop-install-banner");
    expect(card.compareDocumentPosition(installBanner) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
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

  it("shows the push notification CTA when loop push is available", async () => {
    const user = userEvent.setup();
    const enableMock = vi.fn().mockResolvedValue(true);
    useLoopPushNotificationsMock.mockReturnValue({
      supported: true,
      enabledInBackend: true,
      permission: "default",
      isEnabled: false,
      isBusy: false,
      error: null,
      canEnable: true,
      canDisable: false,
      enable: enableMock,
      disable: vi.fn(),
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("loop-push-banner")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("loop-push-enable-action"));

    expect(enableMock).toHaveBeenCalledTimes(1);
  });
});
