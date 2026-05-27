import Foundation
import Testing
@testable import Longhouse

struct SessionLiveActivityModelsTests {
    @Test
    func decodesServerContentStatePayloadKeys() throws {
        let payload = """
        {
          "presenceState": "running",
          "displayPhase": "Running bash",
          "activeTool": "bash",
          "updatedAt": 1777140000,
          "isAttention": false
        }
        """
        let data = try #require(payload.data(using: .utf8))
        let state = try JSONDecoder().decode(SessionWatchAttributes.ContentState.self, from: data)

        #expect(state.presenceState == "running")
        #expect(state.displayPhase == "Running bash")
        #expect(state.activeTool == "bash")
        #expect(state.updatedAt == 1_777_140_000)
        #expect(state.isAttention == false)
    }

    @Test
    func decodesNullActiveToolFromServerPayload() throws {
        let payload = """
        {
          "presenceState": "needs_user",
          "displayPhase": "Idle",
          "activeTool": null,
          "updatedAt": 1777140001,
          "isAttention": false
        }
        """
        let data = try #require(payload.data(using: .utf8))
        let state = try JSONDecoder().decode(SessionWatchAttributes.ContentState.self, from: data)

        #expect(state.presenceState == "needs_user")
        #expect(state.activeTool == nil)
        #expect(state.isAttention == false)
    }

    @Test
    func contentStatePrefersCanonicalServerRuntimeDisplay() throws {
        let json = """
        {
          "id": "session-runtime-shell",
          "provider": "claude",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Run checks",
          "summary_title": "Run Checks",
          "presence_state": "running",
          "presence_tool": "bash",
          "user_state": "active",
          "status": "working",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Running bash",
          "active_tool": "bash",
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true,
            "display_label": "Live on this Mac",
            "display_detail": "Longhouse can send prompts into this live session.",
            "display_tone": "success"
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": "running",
            "tone": "running",
            "headline": "Working",
            "detail": "Using Shell",
            "phase_label": "Using Shell",
            "compact_tool_label": "Shell",
            "is_live": true,
            "is_executing": true,
            "needs_attention": false,
            "is_idle": false,
            "is_stalled": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.displayPhase == "Using Shell")
        #expect(state.activeTool == "Shell")
    }

    @Test
    func contentStateUsesRuntimeDisplayOverRuntimeFacts() throws {
        let json = """
        {
          "id": "session-runtime-facts",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Run checks",
          "summary_title": "Run Checks",
          "presence_state": "running",
          "presence_tool": "bash",
          "user_state": "active",
          "status": "working",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Running bash",
          "active_tool": "bash",
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true,
            "display_label": "Live on this Mac",
            "display_detail": "Longhouse can send prompts into this live session.",
            "display_tone": "success"
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": "running",
            "tone": "running",
            "headline": "Working",
            "detail": "Using Shell",
            "phase_label": "Using Shell",
            "compact_tool_label": "Shell",
            "is_live": true,
            "is_executing": true,
            "needs_attention": true,
            "is_idle": false,
            "is_stalled": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.presenceState == "running")
        #expect(state.displayPhase == "Using Shell")
        #expect(state.activeTool == "Shell")
        #expect(state.isAttention)
    }

    @Test
    func contentStateRendersClosedLifecycleGenericallyRegardlessOfTerminalReason() throws {
        let json = """
        {
          "id": "session-terminal-disconnected",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Closed",
          "summary_title": "Closed",
          "presence_state": "needs_user",
          "presence_tool": null,
          "user_state": "active",
          "status": "working",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Idle",
          "active_tool": null,
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": false,
            "host_reattach_available": true,
            "reply_to_live_session_available": false
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": null,
            "tone": "closed",
            "headline": "Closed",
            "detail": null,
            "phase_label": "Closed",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": true,
            "is_stalled": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "stale",
            "lifecycle": "closed",
            "host_state": "online",
            "terminal_reason": "provider_signal"
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.presenceState == "unknown")
        #expect(state.displayPhase == "Closed")
        #expect(state.activeTool == nil)
        #expect(state.isAttention == false)
    }

    @Test
    func contentStateDoesNotFallbackToStaleTopLevelProgressWhenRuntimeDisplayHasNoState() throws {
        let json = """
        {
          "id": "session-stale-top-level",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Stale progress",
          "summary_title": "Stale Progress",
          "presence_state": "running",
          "presence_tool": "bash",
          "user_state": "active",
          "status": "working",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Running bash",
          "active_tool": "bash",
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": false,
            "host_reattach_available": true,
            "reply_to_live_session_available": false,
            "display_label": "Managed",
            "display_detail": "Control path is offline.",
            "display_tone": "neutral"
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": null,
            "tone": "inactive",
            "headline": "Not connected",
            "detail": null,
            "phase_label": "Inactive",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": false,
            "is_stalled": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.presenceState == "unknown")
        #expect(state.displayPhase == "Inactive")
        #expect(state.activeTool == nil)
        #expect(state.isAttention == false)
    }
}
