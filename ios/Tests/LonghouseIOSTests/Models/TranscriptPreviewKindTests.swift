import XCTest
@testable import Longhouse

/// A preview frame from a server that predates `kind`, and one from a server
/// that sends it, must both decode on the stream path; thinking is never drawn
/// as a reply.
final class TranscriptPreviewKindTests: XCTestCase {
    private func frame(previewExtra: String) -> Data {
        Data("""
        {"session_id":"s1","latest_event_id":-5,"thread_session_count":1,
         "latest_event_emitted_at_ms":null,"server_fanout_at_ms":null,"server_now_ms":null,"pubsub_seq":9,
         "transcript_preview":{"event_id":5,"text":"Partial live answer","role":"assistant",\(previewExtra)
           "tool_name":null,"tool_input_json":null,"tool_output_text":null,"tool_call_id":null,"tool_call_state":null,
           "event_origin":"live_provisional","timestamp":"2026-03-22T22:00:05Z","is_provisional":true,"is_complete":false,
           "content_cursor":"c1","is_stale":false,"stale_reason":null}}
        """.utf8)
    }

    func testOldShapeWithoutKindDecodesAndRendersAsBefore() throws {
        let changed = try JSONDecoder().decode(SessionWorkspaceStream.WorkspaceChanged.self, from: frame(previewExtra: ""))
        let preview = try XCTUnwrap(changed.transcript_preview?.sessionTranscriptPreview)
        XCTAssertNil(preview.kind)
        XCTAssertTrue(preview.shouldRender)
        XCTAssertEqual(preview.syntheticEvents.first?.contentText, "Partial live answer")
    }

    func testEveryKindDecodes() throws {
        for kind in ["reply", "thinking", "tool", "subagent"] {
            let data = frame(previewExtra: "\"kind\":\"\(kind)\",")
            let changed = try JSONDecoder().decode(SessionWorkspaceStream.WorkspaceChanged.self, from: data)
            XCTAssertEqual(changed.transcript_preview?.kind, kind)
            XCTAssertEqual(changed.transcript_preview?.sessionTranscriptPreview.kind, kind)
        }
    }

    func testThinkingIsNeverRenderedAsAReply() throws {
        let data = frame(previewExtra: "\"kind\":\"thinking\",")
        let changed = try JSONDecoder().decode(SessionWorkspaceStream.WorkspaceChanged.self, from: data)
        let preview = try XCTUnwrap(changed.transcript_preview?.sessionTranscriptPreview)
        XCTAssertFalse(preview.shouldRender)
        XCTAssertTrue(TranscriptPreviewProjection.visibleEvents(durableEvents: [], preview: preview).isEmpty)
    }

    func testCachedPreviewWithoutKindStillDecodes() throws {
        let cached = Data("""
        {"eventId":1,"text":"hi","role":"assistant","toolName":null,"toolInputJSON":null,"toolOutputText":null,
         "toolCallId":null,"toolCallState":null,"eventOrigin":"live_provisional","timestamp":"2026-03-22T22:00:05Z",
         "isProvisional":true,"isComplete":false,"contentCursor":null,"isStale":false,"staleReason":null}
        """.utf8)
        let preview = try JSONDecoder().decode(SessionTranscriptPreview.self, from: cached)
        XCTAssertNil(preview.kind)
        XCTAssertTrue(preview.shouldRender)
    }
}
