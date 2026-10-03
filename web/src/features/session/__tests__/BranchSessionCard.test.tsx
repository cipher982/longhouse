import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BranchSessionCard } from "../BranchSessionCard";

const createSessionBranch = vi.fn();

vi.mock("@/shared/api/agents", () => ({
  createSessionBranch: (...args: unknown[]) => createSessionBranch(...args),
}));

function renderCard(overrides: Partial<Parameters<typeof BranchSessionCard>[0]> = {}) {
  const onBranched = vi.fn();
  render(
    <BranchSessionCard
      sessionId="session-1"
      providerLabel="Codex"
      machineLabel="cinder"
      available
      onBranched={onBranched}
      {...overrides}
    />,
  );
  return { onBranched };
}

describe("BranchSessionCard", () => {
  beforeEach(() => {
    createSessionBranch.mockReset();
  });

  it("sends the typed message and hands back the new session", async () => {
    createSessionBranch.mockResolvedValue({ session_id: "branch-1" });
    const { onBranched } = renderCard();

    await userEvent.type(screen.getByTestId("branch-session-input"), "keep going");
    await userEvent.click(screen.getByTestId("branch-session-submit"));

    await waitFor(() => expect(onBranched).toHaveBeenCalledWith("branch-1"));
    const [sessionId, body] = createSessionBranch.mock.calls[0];
    expect(sessionId).toBe("session-1");
    expect(body.message).toBe("keep going");
    // A retry after a dropped response must not start a second branch, and the
    // server deduplicates on exactly this.
    expect(body.client_request_id).toBeTruthy();
  });

  it("will not submit an empty message", async () => {
    renderCard();
    expect(screen.getByTestId("branch-session-submit")).toBeDisabled();
    await userEvent.type(screen.getByTestId("branch-session-input"), "   ");
    expect(screen.getByTestId("branch-session-submit")).toBeDisabled();
    expect(createSessionBranch).not.toHaveBeenCalled();
  });

  it("keeps the draft when the branch fails", async () => {
    createSessionBranch.mockRejectedValue(new Error("nope"));
    const { onBranched } = renderCard();

    await userEvent.type(screen.getByTestId("branch-session-input"), "keep going");
    await userEvent.click(screen.getByTestId("branch-session-submit"));

    // Losing what someone typed because the request failed is the worst
    // possible response to a failure they can simply retry.
    await screen.findByTestId("branch-session-error");
    expect(screen.getByTestId("branch-session-input")).toHaveValue("keep going");
    expect(onBranched).not.toHaveBeenCalled();
  });

  it("says why an approval-bearing session cannot be branched", () => {
    renderCard({ available: false, unavailableReason: "permission_mode_unsupported" });
    expect(screen.getByTestId("branch-session-unavailable")).toHaveTextContent(/ran with them/i);
    expect(screen.queryByTestId("branch-session-input")).not.toBeInTheDocument();
  });

  it.each(["fork_unsupported", "machine_offline", "contract_invalid", undefined])(
    "stays silent for %s, which the rest of the screen already explains or nobody can act on",
    (reason) => {
      const { container } = render(
        <BranchSessionCard
          sessionId="session-1"
          providerLabel="Claude"
          machineLabel="cinder"
          available={false}
          unavailableReason={reason}
          onBranched={vi.fn()}
        />,
      );
      expect(container).toBeEmptyDOMElement();
    },
  );

  it("reuses the request id when the same text is retried, so a lost response cannot start two branches", async () => {
    createSessionBranch.mockRejectedValueOnce(new Error("dropped")).mockResolvedValueOnce({ session_id: "branch-1" });
    const { onBranched } = renderCard();

    await userEvent.type(screen.getByTestId("branch-session-input"), "keep going");
    await userEvent.click(screen.getByTestId("branch-session-submit"));
    await screen.findByTestId("branch-session-error");
    await userEvent.click(screen.getByTestId("branch-session-submit"));
    await waitFor(() => expect(onBranched).toHaveBeenCalledWith("branch-1"));

    expect(createSessionBranch.mock.calls[1][1].client_request_id).toBe(createSessionBranch.mock.calls[0][1].client_request_id);
  });

  it("uses a new request id when the text changes", async () => {
    createSessionBranch.mockRejectedValue(new Error("nope"));
    renderCard();

    await userEvent.type(screen.getByTestId("branch-session-input"), "one");
    await userEvent.click(screen.getByTestId("branch-session-submit"));
    await screen.findByTestId("branch-session-error");
    await userEvent.type(screen.getByTestId("branch-session-input"), " two");
    await userEvent.click(screen.getByTestId("branch-session-submit"));
    await waitFor(() => expect(createSessionBranch).toHaveBeenCalledTimes(2));

    expect(createSessionBranch.mock.calls[1][1].client_request_id).not.toBe(
      createSessionBranch.mock.calls[0][1].client_request_id,
    );
  });
});
