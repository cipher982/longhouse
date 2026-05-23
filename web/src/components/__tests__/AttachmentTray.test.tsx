import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AttachmentTray } from "../AttachmentTray";
import type { ComposerAttachment } from "../../lib/useComposerAttachments";

function attach(id: string, name = `${id}.png`): ComposerAttachment {
  return {
    clientId: id,
    filename: name,
    mimeType: "image/webp",
    byteSize: 1024,
    previewUrl: `blob://${id}`,
    blob: new Blob(["x"], { type: "image/webp" }),
  };
}

describe("AttachmentTray", () => {
  it("renders a thumbnail per attachment with a remove button", () => {
    const onRemove = vi.fn();
    render(
      <AttachmentTray
        attachments={[attach("1"), attach("2")]}
        onAddFiles={vi.fn()}
        onRemove={onRemove}
      />,
    );
    expect(screen.getAllByRole("img")).toHaveLength(2);
    fireEvent.click(screen.getByLabelText("Remove 1.png"));
    expect(onRemove).toHaveBeenCalledWith("1");
  });

  it("hides the add button once the cap is reached", () => {
    render(
      <AttachmentTray
        attachments={[attach("1"), attach("2"), attach("3"), attach("4")]}
        onAddFiles={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Attach images")).toBeNull();
  });

  it("can disable adding without trapping existing attachments", () => {
    const onRemove = vi.fn();
    render(
      <AttachmentTray
        attachments={[attach("1")]}
        onAddFiles={vi.fn()}
        onRemove={onRemove}
        addDisabled
      />,
    );

    expect(screen.queryByLabelText("Attach images")).toBeNull();
    fireEvent.click(screen.getByLabelText("Remove 1.png"));
    expect(onRemove).toHaveBeenCalledWith("1");
  });

  it("surfaces compressor errors", () => {
    render(
      <AttachmentTray
        attachments={[]}
        onAddFiles={vi.fn()}
        onRemove={vi.fn()}
        error="too big"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("too big");
  });
});
