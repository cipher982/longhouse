import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TestRouter } from "@/shared/test/test-utils";
import AddRunnerModal from "../AddRunnerModal";

const enrollMocks = vi.hoisted(() => ({ mutate: vi.fn(), state: { data: undefined as unknown } }));

vi.mock("../useRunners", () => ({
  useCreateEnrollToken: () => ({
    mutate: enrollMocks.mutate,
    data: enrollMocks.state.data,
    isPending: false,
    error: null,
  }),
}));

function renderModal() {
  return render(
    <TestRouter>
      <AddRunnerModal isOpen onClose={() => {}} />
    </TestRouter>,
  );
}

describe("AddRunnerModal", () => {
  beforeEach(() => {
    enrollMocks.mutate.mockClear();
    enrollMocks.state.data = undefined;
  });

  it("leads with connecting the machine and mints no Runner token until asked", async () => {
    renderModal();

    expect(screen.getByTestId("connect-machine-command")).toHaveTextContent(
      `LONGHOUSE_URL='${window.location.origin}'`,
    );
    expect(screen.queryByTestId("add-runner-command")).toBeNull();
    expect(enrollMocks.mutate).not.toHaveBeenCalled();

    await userEvent.click(screen.getByTestId("add-runner-optional-toggle"));
    expect(enrollMocks.mutate).toHaveBeenCalledTimes(1);
  });
});
