import Foundation

@testable import Longhouse

/// Shared builder for a minimal single-event `SessionWorkspaceResponse` used by
/// resume/stream tests. Mirrors the inline JSON in `SessionViewModelTests`.
enum TestWorkspaceFactory {
    static func make(
        eventId: Int,
        content: String,
        total: Int = 1,
        pageOffset: Int = 0
    ) throws -> SessionWorkspaceResponse {
        let encodedContent = try jsonString(content)
        let json = """
        {
          "session": {
            "id": "session-1",
            "provider": "codex",
            "project": "zerg",
            "summary_title": "Workspace Session",
            "user_state": "active",
            "capabilities": {
              "live_control_available": true,
              "host_reattach_available": true,
              "reply_to_live_session_available": true
            },
            "runtime_display": {
              "truth_tier": "fresh",
              "signal_tier": "none",
              "state": null,
              "tone": "inactive",
              "headline": "Inactive",
              "detail": null,
              "phase_label": "Inactive",
              "compact_tool_label": null,
              "is_live": false,
              "is_executing": false,
              "needs_attention": false,
              "is_idle": true,
              "is_stalled": false,
              "is_managed_local_truth": false,
              "has_signal": false,
              "control_path": "unmanaged",
              "activity_recency": "none",
              "lifecycle": "open",
              "host_state": "unknown",
              "terminal_reason": null
            },
            "loop_mode": "assist"
          },
          "thread": {
            "root_session_id": "session-1",
            "head_session_id": "session-1",
            "sessions": []
          },
          "projection": {
            "root_session_id": "session-1",
            "focus_session_id": "session-1",
            "head_session_id": "session-1",
            "path_session_ids": ["session-1"],
            "items": [
              {
                "kind": "event",
                "session_id": "session-1",
                "timestamp": "2026-05-02T20:00:00Z",
                "event": {
                  "id": \(eventId),
                  "role": "user",
                  "content_text": \(encodedContent),
                  "timestamp": "2026-05-02T20:00:00Z",
                  "in_active_context": true,
                  "is_head_branch": true
                }
              }
            ],
            "total": \(total),
            "page_offset": \(pageOffset),
            "branch_mode": "head",
            "abandoned_events": 0
          }
        }
        """.data(using: .utf8)!
        return try JSONDecoder.snakeCase.decode(SessionWorkspaceResponse.self, from: json)
    }

    static func jsonString(_ value: String) throws -> String {
        let data = try JSONEncoder().encode(value)
        return String(data: data, encoding: .utf8)!
    }
}
