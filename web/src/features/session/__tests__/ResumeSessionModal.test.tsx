import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SessionResumeIntent } from "@/shared/api/agents";
import {
  formatRunEndReason,
  isUnexpectedResumeStop,
  ResumeSessionModal,
} from "../ResumeSessionModal";

const BASE_INTENT: SessionResumeIntent = {
  session_id: "2b78c4f2-2667-4158-8f28-1b41f452d8bc",
  provider: "claude",
  machine_id: "cinder",
  machine_label: "cinder",
  cwd: "/Users/david/repo",
  available: true,
  reason: null,
  argv: [
    "longhouse",
    "claude",
    "--cwd",
    "/Users/david/repo",
    "--resume",
    "2b78c4f2-2667-4158-8f28-1b41f452d8bc",
  ],
  command:
    "longhouse claude --cwd /Users/david/repo --resume 2b78c4f2-2667-4158-8f28-1b41f452d8bc",
  handoff: "terminal_command",
};

describe("isUnexpectedResumeStop", () => {
  it.each(["provider_exit", "process_gone", "provider_signal", "owner_gone"])(
    "treats %s as an unexpected stop",
    (reason) => {
      expect(isUnexpectedResumeStop(reason)).toBe(true);
    },
  );

  it.each(["user_closed", "remote_terminate", "bridge_stop", "exit_0", null, undefined])(
    "treats %s as an expected or clean stop",
    (reason) => {
      expect(isUnexpectedResumeStop(reason)).toBe(false);
    },
  );
});

describe("formatRunEndReason", () => {
  it("formats factual Helm run end reasons", () => {
    expect(formatRunEndReason("user_closed")).toBe("Exited by user");
    expect(formatRunEndReason("remote_terminate")).toBe("Stopped remotely");
    expect(formatRunEndReason("bridge_stop")).toBe("Stopped cleanly");
    expect(formatRunEndReason("provider_signal")).toBe("Stopped by signal");
    expect(formatRunEndReason("provider_exit")).toBe("Stopped unexpectedly");
    expect(formatRunEndReason("process_gone")).toBe("Stopped unexpectedly");
    expect(formatRunEndReason("exit_0")).toBeNull();
    expect(formatRunEndReason(null)).toBeNull();
  });
});

describe("ResumeSessionModal", () => {
  it("shows recovery copy for unexpected stops and omits it for clean user exits", () => {
    const { rerender } = render(
      <ResumeSessionModal
        intent={BASE_INTENT}
        unexpectedStop={isUnexpectedResumeStop("provider_signal")}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByTestId("resume-session-recovery-copy")).toHaveTextContent(
      /stopped unexpectedly/i,
    );

    rerender(
      <ResumeSessionModal
        intent={BASE_INTENT}
        unexpectedStop={isUnexpectedResumeStop("user_closed")}
        onClose={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("resume-session-recovery-copy"),
    ).not.toBeInTheDocument();
  });
});
