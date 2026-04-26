import Foundation
import Testing
@testable import Longhouse

struct SessionModelsTests {
    @Test
    func sessionDetailDecodesLoopModeAndCockpitState() throws {
        let json = """
        {
          "id": "session-1",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Fix mobile control",
          "summary_title": "Mobile Control",
          "presence_state": "needs_user",
          "presence_tool": null,
          "user_state": "active",
          "status": "working",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Waiting on you",
          "active_tool": null,
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.effectiveLoopMode == .assist)
        #expect(detail.canSendLive)
        #expect(detail.cockpitPhaseLabel == "Waiting on you")
        #expect(detail.controlHealthMessage == nil)
    }

    @Test
    func sessionDetailMarksImportedSessionsReadOnly() throws {
        let json = """
        {
          "id": "session-2",
          "provider": "gemini",
          "project": "bar",
          "cwd": null,
          "git_branch": null,
          "summary": null,
          "summary_title": null,
          "presence_state": "idle",
          "presence_tool": null,
          "user_state": "active",
          "status": "idle",
          "last_activity_at": null,
          "display_phase": null,
          "active_tool": null,
          "home_label": null,
          "origin_label": null,
          "capabilities": {
            "live_control_available": false,
            "host_reattach_available": false,
            "reply_to_live_session_available": false
          },
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.isReadOnly)
        #expect(detail.controlHealthMessage == "Read-only imported session.")
        #expect(detail.cockpitPhaseLabel == "Idle")
    }
}
