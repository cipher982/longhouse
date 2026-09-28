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
          "cwd": "/Users/example/git/zerg",
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
          }
        }
        """.data(using: .utf8)!

        let data = try addingSessionStateFacts(
            makeSessionStateFacts(activity: "executing", tool: "Shell"),
            to: json
        )
        let detail = try JSONDecoder.snakeCase.decodeSessionFixture(SessionDetail.self, from: data)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.displayPhase == "Using Shell")
        #expect(state.activeTool == "Shell")
    }

    @Test
    func contentStateDoesNotFallbackToStaleTopLevelProgressWhenRuntimeDisplayHasNoState() throws {
        let json = """
        {
          "id": "session-stale-top-level",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/example/git/zerg",
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
          }
        }
        """.data(using: .utf8)!

        let data = try addingSessionStateFacts(
            makeSessionStateFacts(activity: "unknown"),
            to: json
        )
        let detail = try JSONDecoder.snakeCase.decodeSessionFixture(SessionDetail.self, from: data)
        let state = detail.liveActivityContentState(updatedAt: Date(timeIntervalSince1970: 1_777_140_000))

        #expect(state.presenceState == "unknown")
        #expect(state.displayPhase == "Activity unknown")
        #expect(state.activeTool == nil)
        #expect(state.isAttention == false)
    }

    private func contentState(_ presence: String, attention: Bool = false) -> SessionWatchAttributes.ContentState {
        SessionWatchAttributes.ContentState(
            presenceState: presence,
            displayPhase: "",
            activeTool: nil,
            updatedAt: 1_777_140_000,
            isAttention: attention
        )
    }

    /// Every served activity state, as the app writes it, gets the app's own
    /// signal and a real word -- never "?".
    @Test(arguments: [
        ("thinking", TimelineSignal.working, "Think"),
        ("executing", TimelineSignal.working, "Run"),
        ("quiescent", TimelineSignal.quiet, "Idle"),
        ("blocked", TimelineSignal.attention, "Hold"),
        ("stalled", TimelineSignal.attention, "Stall"),
        ("unknown", TimelineSignal.unknown, "Unknown"),
    ])
    func everyActivityStateMapsLikeTheApp(presence: String, signal: TimelineSignal, word: String) {
        let state = contentState(presence)
        #expect(state.activityState == presence)
        #expect(state.signal == signal)
        #expect(state.signal == TimelineSignal.forActivityState(presence))
        #expect(state.compactStateLabel == word)
        #expect(state.compactStateLabel != "?")
    }

    /// The server's Live Activity push renames executing and quiescent to
    /// presence words; they fold back to the same state the app writes.
    @Test(arguments: [
        ("running", "executing", "Run"),
        ("idle", "quiescent", "Idle"),
        ("needs_user", "quiescent", "Idle"),
    ])
    func serverPushAliasesFoldBackToActivityStates(presence: String, activity: String, word: String) {
        let state = contentState(presence)
        #expect(state.activityState == activity)
        #expect(state.signal == TimelineSignal.forActivityState(activity))
        #expect(state.compactStateLabel == word)
    }

    @Test
    func aPendingInteractionIsAttentionWhateverTheActivity() {
        for presence in ["thinking", "executing", "quiescent", "unknown"] {
            let state = contentState(presence, attention: true)
            #expect(state.signal == .attention)
            #expect(state.compactStateLabel == "Needs you")
        }
    }

}
