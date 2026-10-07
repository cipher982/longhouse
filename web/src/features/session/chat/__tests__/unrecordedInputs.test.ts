import { describe, expect, it } from "vitest";
import type { QueuedInputSummary } from "@/shared/api/sessionChat";
import type { TimelineItem } from "@/shared/session/model";
import {
  failedBeforeRecorded,
  isSettledDelivery,
  normalizeInputText,
  receiptsShownByTranscript,
} from "../unrecordedInputs";

function receipt(overrides: Partial<QueuedInputSummary>): QueuedInputSummary {
  return {
    text: "keep going",
    intent: "auto",
    status: "delivered",
    created_at: "2026-10-07T13:17:33Z",
    ...overrides,
  };
}

function userRow(id: number, text: string, timestamp: string, extra: object = {}): TimelineItem {
  return {
    kind: "message",
    event: {
      id,
      role: "user",
      content_text: text,
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp,
      in_active_context: true,
      ...extra,
    },
  };
}

describe("unrecorded inputs", () => {
  it("settles a delivered receipt once no Console turn runs on it", () => {
    expect(isSettledDelivery(receipt({ turn: null }))).toBe(true);
    expect(isSettledDelivery(receipt({ turn: { turn_id: "t", state: "completed" } }))).toBe(true);
    expect(isSettledDelivery(receipt({ turn: { turn_id: "t", state: "active" } }))).toBe(false);
    expect(isSettledDelivery(receipt({ status: "queued" }))).toBe(false);
    expect(failedBeforeRecorded(receipt({ turn: { turn_id: "t", state: "failed" } }))).toBe(true);
    expect(failedBeforeRecorded(receipt({ turn: { turn_id: "t", state: "completed" } }))).toBe(false);
  });

  it("normalizes text the way the server's linker does", () => {
    expect(
      normalizeInputText(
        '<channel source="longhouse-channel" intent="steer">\n  look   at\nthis [image attached: a.png]\n</channel>',
      ),
    ).toBe("look at this");
  });

  it("pairs a resend with its transcript row, leaving the lost original unshown", () => {
    // Bug C: the 13:17 send was lost, the 15:01 resend of the same words was
    // recorded once, and the server linked neither (ambiguous text).
    const lost = receipt({ client_request_id: "ios-a", created_at: "2026-10-07T13:17:33Z" });
    const resend = receipt({ client_request_id: "web-b", created_at: "2026-10-07T15:01:28Z" });
    const shown = receiptsShownByTranscript(
      [lost, resend],
      [userRow(1, "keep  going", "2026-10-07T15:01:30Z")],
    );
    expect(shown.has(resend)).toBe(true);
    expect(shown.has(lost)).toBe(false);
  });

  it("never pairs a receipt with a row written before it was sent", () => {
    const later = receipt({ created_at: "2026-10-07T13:17:33Z" });
    const shown = receiptsShownByTranscript([later], [userRow(1, "keep going", "2026-10-07T12:00:00Z")]);
    expect(shown.size).toBe(0);
  });

  it("lets a row stamped with a request id stand only for that request", () => {
    const owner = receipt({ client_request_id: "web-owner", created_at: "2026-10-07T13:00:00Z" });
    const other = receipt({ client_request_id: "ios-other", created_at: "2026-10-07T13:00:01Z" });
    const shown = receiptsShownByTranscript(
      [owner, other],
      [
        userRow(1, "keep going", "2026-10-07T13:00:05Z", {
          input_origin: { authored_via: "longhouse", client_request_id: "web-owner" },
        }),
      ],
    );
    expect(shown.has(owner)).toBe(true);
    expect(shown.has(other)).toBe(false);
  });
});
