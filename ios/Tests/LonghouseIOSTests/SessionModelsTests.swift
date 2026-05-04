import Foundation
import Testing
@testable import Longhouse

struct SessionModelsTests {
    @Test
    func sessionWorkspaceDecodesThreadProjectionEventsAndSeams() throws {
        let json = """
        {
          "session": {
            "id": "session-child",
            "provider": "codex",
            "project": "zerg",
            "cwd": "/Users/davidrose/git/zerg",
            "git_branch": "main",
            "summary": "Move iOS to workspace",
            "summary_title": "iOS Workspace",
            "presence_state": "idle",
            "presence_tool": null,
            "user_state": "active",
            "status": "idle",
            "last_activity_at": "2026-05-02T20:00:00Z",
            "display_phase": "Idle",
            "active_tool": null,
            "home_label": "On this Mac",
            "origin_label": "On this Mac",
            "capabilities": {
              "live_control_available": true,
              "host_reattach_available": true,
              "reply_to_live_session_available": true,
              "can_queue_next_input": true,
              "can_steer_active_turn": false,
              "display_label": "Live on this Mac",
              "display_detail": "Longhouse can send prompts into this live session.",
              "display_tone": "success"
            },
            "runtime_display": {
              "truth_tier": "managed-local",
              "state": "idle",
              "tone": "idle",
              "headline": "Ready",
              "detail": null,
              "phase_label": "Idle",
              "compact_tool_label": null,
              "is_live": false,
              "is_executing": false,
              "needs_attention": false,
              "is_idle": true,
              "heuristic_active": false,
              "is_managed_local_truth": true,
              "has_signal": true,
              "control_path": "managed",
              "activity_recency": "recent",
              "lifecycle": "open",
              "host_state": "online",
              "terminal_reason": null
            },
            "loop_mode": "assist"
          },
          "thread": {
            "root_session_id": "session-root",
            "head_session_id": "session-child",
            "sessions": [
              {
                "id": "session-child",
                "provider": "codex",
                "project": "zerg",
                "user_state": "active",
                "capabilities": {
                  "live_control_available": true,
                  "host_reattach_available": true,
                  "reply_to_live_session_available": true
                }
              }
            ]
          },
          "projection": {
            "root_session_id": "session-root",
            "focus_session_id": "session-child",
            "head_session_id": "session-child",
            "path_session_ids": ["session-root", "session-child"],
            "items": [
              {
                "kind": "event",
                "session_id": "session-root",
                "timestamp": "2026-05-02T19:59:00Z",
                "event": {
                  "id": 10,
                  "role": "user",
                  "content_text": "Can you migrate iOS?",
                  "tool_name": null,
                  "tool_input_json": null,
                  "tool_output_text": null,
                  "tool_call_id": null,
                  "timestamp": "2026-05-02T19:59:00Z",
                  "in_active_context": true,
                  "is_head_branch": true
                }
              },
              {
                "kind": "seam",
                "session_id": "session-child",
                "timestamp": "2026-05-02T20:00:00Z",
                "continued_from_session_id": "session-root",
                "continuation_kind": "local",
                "origin_label": "On this Mac",
                "parent_origin_label": "Hosted",
                "parent_continuation_kind": "cloud",
                "branched_from_event_id": 10
              }
            ],
            "total": 2,
            "page_offset": 0,
            "branch_mode": "head",
            "abandoned_events": 0
          }
        }
        """.data(using: .utf8)!

        let workspace = try JSONDecoder.snakeCase.decode(SessionWorkspaceResponse.self, from: json)

        #expect(workspace.session.id == "session-child")
        #expect(workspace.thread.rootSessionId == "session-root")
        #expect(workspace.thread.sessions.map(\.id) == ["session-child"])
        #expect(workspace.projection.pathSessionIds == ["session-root", "session-child"])
        #expect(workspace.projection.items.map(\.kind) == ["event", "seam"])
        #expect(workspace.projection.items[0].id == "event:10")
        #expect(workspace.projection.items[1].id == "seam:session-child:2026-05-02T20:00:00Z")
        #expect(workspace.projection.items[1].continuedFromSessionId == "session-root")
        #expect(workspace.events.map(\.id) == [10])
    }

    @Test
    func timelineBranchBadgeUsesOnlyRealGitBranch() {
        let branchSession = SessionSummary(
            id: "session-branch",
            title: "Branch session",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: nil,
            gitBranch: " main ",
            homeLabel: "cinder",
            headOriginLabel: "Cloud"
        )
        let originOnlySession = SessionSummary(
            id: "session-origin",
            title: "Origin session",
            presenceState: "idle",
            provider: "opencode",
            project: "sauron-email-agent",
            lastActivityAt: nil,
            gitBranch: nil,
            homeLabel: "cinder",
            headOriginLabel: "email:Sauron production-mode validation"
        )
        let headSession = SessionSummary(
            id: "session-head",
            title: "Detached session",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: nil,
            gitBranch: "HEAD"
        )

        #expect(branchSession.timelineBranchBadgeLabel == "main")
        #expect(originOnlySession.timelineBranchBadgeLabel == nil)
        #expect(headSession.timelineBranchBadgeLabel == nil)
    }

    @Test
    func sessionDetailDecodesLoopModeAndRuntimeState() throws {
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
          "status": "idle",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Ready",
          "active_tool": null,
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
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.effectiveLoopMode == .assist)
        #expect(detail.canSendLive)
        #expect(detail.runtimeCapabilityLabel == "Live on this Mac")
        #expect(detail.runtimeCapabilityTone == "success")
        #expect(detail.runtimePhaseLabel == "Ready")
        #expect(detail.controlHealthMessage == nil)
    }

    @Test
    func sessionDetailPrefersServerRuntimeDisplay() throws {
        let json = """
        {
          "id": "session-3",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Run live checks",
          "summary_title": "Live Checks",
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
            "can_queue_next_input": true,
            "display_label": "Live on this Mac",
            "display_detail": "Longhouse can send prompts into this live session.",
            "display_tone": "success"
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "state": "running",
            "tone": "running",
            "headline": "Working",
            "detail": "Running Shell",
            "phase_label": "Running Shell",
            "compact_tool_label": "Shell",
            "is_live": true,
            "is_executing": true,
            "needs_attention": false,
            "is_idle": false,
            "heuristic_active": false,
            "is_managed_local_truth": true,
            "has_signal": true
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimeHeadline == "Working")
        #expect(detail.runtimeDetail == "Running Shell")
        #expect(detail.runtimeCapabilityLabel == "Live on this Mac")
        #expect(detail.runtimePhaseLabel == "Running Shell")
        #expect(detail.runtimeTone == "running")
        #expect(detail.isSessionExecuting)
    }

    @Test
    func sessionDetailRuntimeDisplayNilStateSuppressesStalePresenceState() throws {
        let json = """
        {
          "id": "session-stale-detail",
          "provider": "codex",
          "project": "zerg",
          "user_state": "active",
          "presence_state": "needs_user",
          "status": "active",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "state": null,
            "tone": "inactive",
            "headline": "Not connected",
            "detail": null,
            "phase_label": "Recent",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": false,
            "heuristic_active": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "stale",
            "lifecycle": "open",
            "host_state": "unknown",
            "terminal_reason": null
          }
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimePhaseState == "idle")
        #expect(detail.runtimeHeadline == "Not connected")
        #expect(detail.runtimeTone == "inactive")
        #expect(!detail.isSessionExecuting)
    }

    @Test
    func sessionDetailCanonicalizesLegacyShellLabels() throws {
        let json = """
        {
          "id": "session-shell",
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
          "display_phase": "running bash",
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
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimePhaseLabel == "Running Shell")
        #expect(detail.runtimeDetail == "Running Shell")
    }

    @Test
    func runtimeDisplayTextCanonicalizesOnlyBareShellAliases() {
        #expect(RuntimeDisplayText.canonicalDisplayText("Running bash") == "Running Shell")
        #expect(RuntimeDisplayText.canonicalDisplayText("Blocked on terminal") == "Blocked on Shell")
        #expect(RuntimeDisplayText.canonicalDisplayText("Approval needed \u{2022} shell") == "Approval needed \u{2022} Shell")
        #expect(RuntimeDisplayText.canonicalDisplayText("Running bash-runner") == "Running bash-runner")
        #expect(RuntimeDisplayText.canonicalDisplayText("Running bash script") == "Running bash script")
    }

    @Test
    func sessionSummaryCanonicalizesLegacyShellLabels() {
        let summary = SessionSummary(
            id: "session-shell",
            title: "Run checks",
            presenceState: "running",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "working",
            displayPhase: "Running bash",
            presenceTool: "bash",
            activeTool: "bash"
        )

        #expect(summary.displayPhaseLabel == "Running Shell")
    }

    @Test
    func closedRuntimeDisplayDoesNotNeedAttention() {
        let summary = SessionSummary(
            id: "session-closed-attention",
            title: "Finished work",
            presenceState: "needs_user",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "active",
            displayPhase: "Ready",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Ready",
                detail: "Ready for next prompt",
                phaseLabel: "Ready",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: true,
                isIdle: false,
                heuristicActive: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "closed",
                hostState: "online",
                terminalReason: "provider_signal"
            )
        )

        #expect(summary.isClosed)
        #expect(!summary.needsAttention)
        #expect(!summary.isExecuting)
        #expect(summary.isIdle)
        #expect(summary.displayPhaseLabel == "Completed")
    }

    @Test
    func runtimeDisplayNilStateSuppressesStaleTopLevelAttention() {
        let summary = SessionSummary(
            id: "session-disconnected-stale-attention",
            title: "Disconnected work",
            presenceState: "needs_user",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "active",
            displayPhase: "Ready",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: nil,
                tone: "inactive",
                headline: "Not connected",
                detail: nil,
                phaseLabel: "Recent",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: false,
                heuristicActive: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "stale",
                lifecycle: "open",
                hostState: "unknown",
                terminalReason: nil
            )
        )

        #expect(!summary.isBlocked)
        #expect(!summary.needsAttention)
        #expect(!summary.isExecuting)
        #expect(summary.runtimeTone == "inactive")
        #expect(summary.displayPhaseLabel == "Recent")
    }

    @Test
    func timelineStatusMatchesUnmanagedRuntimeRecency() {
        let stale = SessionSummary(
            id: "session-stale-unmanaged",
            title: "Imported session",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "stale",
                state: nil,
                tone: "inactive",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Inactive",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                heuristicActive: false,
                isManagedLocalTruth: false,
                hasSignal: true,
                controlPath: "unmanaged",
                activityRecency: "stale",
                lifecycle: "open",
                hostState: "unknown",
                terminalReason: nil
            )
        )
        let live = SessionSummary(
            id: "session-live-unmanaged",
            title: "Imported session",
            presenceState: "needs_user",
            provider: "claude",
            project: "sauron",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            displayPhase: "Ready",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "fresh",
                state: "needs_user",
                tone: "idle",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Ready",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                heuristicActive: false,
                isManagedLocalTruth: false,
                hasSignal: true,
                controlPath: "unmanaged",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "unknown",
                terminalReason: nil
            )
        )

        #expect(stale.timelineStatusLabel == "Stale")
        #expect(stale.timelineStatusSeenAt == "2026-04-25T20:00:00Z")
        #expect(live.timelineStatusLabel == "Active")
    }

    @Test
    func timelineStatusKeepsManagedReadySeparateFromUnmanagedActivity() {
        let summary = SessionSummary(
            id: "session-managed-ready",
            title: "Managed session",
            presenceState: "needs_user",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            displayPhase: "Ready",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Ready",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                heuristicActive: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            )
        )

        #expect(summary.timelineStatusLabel == "Ready")
    }

    @Test
    func sessionSummaryShowsManagedAxisNotLiveControlCapability() {
        let summary = SessionSummary(
            id: "session-control-offline",
            title: "Offline managed session",
            presenceState: "needs_user",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            liveControlAvailable: false,
            hostReattachAvailable: true,
            replyToLiveSessionAvailable: false
        )

        #expect(summary.managementLabel == "Managed")
        #expect(summary.managementTone == "neutral")
    }

    @Test
    func runtimeDisplayNeedsAttentionIsAuthoritativeOverState() {
        let summary = SessionSummary(
            id: "session-stalled-managed",
            title: "Finished work",
            presenceState: "needs_user",
            provider: "codex",
            project: "bar",
            lastActivityAt: "2026-04-28T13:21:51Z",
            status: "active",
            displayPhase: "Ready",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "stale",
                state: "needs_user",
                tone: "idle",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Recent",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: false,
                heuristicActive: false,
                isManagedLocalTruth: false,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "stale",
                lifecycle: "open",
                hostState: "unknown",
                terminalReason: nil
            )
        )

        #expect(!summary.needsAttention)
    }

    @Test
    func attentionWidgetOrderKeepsClosedStaleAttentionOutOfAttentionGroup() {
        let closed = SessionSummary(
            id: "session-closed-process-gone",
            title: "Finished work",
            presenceState: "needs_user",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "active",
            displayPhase: "Ready",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Ready",
                detail: "Ready for next prompt",
                phaseLabel: "Ready",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: true,
                isIdle: false,
                heuristicActive: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "stale",
                lifecycle: "closed",
                hostState: "offline",
                terminalReason: "process_gone"
            )
        )
        let openAttention = SessionSummary(
            id: "session-open-attention",
            title: "Needs reply",
            presenceState: "needs_user",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:01:00Z",
            status: "active",
            displayPhase: "Ready"
        )

        let ordered = SessionSummary.attentionWidgetOrder([closed, openAttention], limit: 2)

        #expect(!closed.needsAttention)
        #expect(!openAttention.needsAttention)
        #expect(ordered.map(\.id) == ["session-closed-process-gone", "session-open-attention"])
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
            "reply_to_live_session_available": false,
            "display_label": "Read only",
            "display_detail": "This imported session is searchable, but Longhouse cannot steer it.",
            "display_tone": "neutral"
          },
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.isReadOnly)
        #expect(detail.runtimeCapabilityLabel == "Read only")
        #expect(detail.runtimeCapabilityTone == "neutral")
        #expect(detail.runtimeHeadline == "Read only")
        #expect(detail.runtimeDetail == "This imported session is searchable, but Longhouse cannot steer it.")
        #expect(detail.controlHealthMessage == "This imported session is searchable, but Longhouse cannot steer it.")
        #expect(detail.runtimePhaseLabel == "Idle")
    }

    @Test
    func sessionDetailNormalizesLegacyCapabilityLabels() throws {
        let liveJSON = """
        {
          "id": "session-live-legacy",
          "provider": "codex",
          "project": "zerg",
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
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true,
            "display_label": "Live control",
            "display_detail": null,
            "display_tone": "success"
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!
        let readOnlyJSON = """
        {
          "id": "session-readonly-legacy",
          "provider": "gemini",
          "project": "zerg",
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
            "reply_to_live_session_available": false,
            "display_label": "Search only",
            "display_detail": null,
            "display_tone": "neutral"
          },
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let liveDetail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: liveJSON)
        let readOnlyDetail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: readOnlyJSON)

        #expect(liveDetail.runtimeCapabilityLabel == "Send")
        #expect(liveDetail.runtimeCapabilityTone == "success")
        #expect(readOnlyDetail.runtimeCapabilityLabel == "Read only")
        #expect(readOnlyDetail.runtimeCapabilityTone == "neutral")
        #expect(readOnlyDetail.runtimeHeadline == "Read only")
    }

    @Test
    func sessionDetailOmitsRedundantIdleRuntimeDetail() throws {
        let json = """
        {
          "id": "session-idle",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Idle session",
          "summary_title": "Idle session",
          "presence_state": "idle",
          "presence_tool": null,
          "user_state": "active",
          "status": "idle",
          "last_activity_at": null,
          "display_phase": null,
          "active_tool": null,
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
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimeHeadline == "Ready")
        #expect(detail.runtimeDetail == nil)
        #expect(detail.runtimePhaseLabel == "Idle")
    }

    @Test
    func sessionInputResponseDecodesSentOutcome() throws {
        let json = """
        {
          "outcome": "sent",
          "input_id": 42,
          "intent": "auto",
          "queued": []
        }
        """.data(using: .utf8)!

        let response = try JSONDecoder.snakeCase.decode(SessionInputResponse.self, from: json)
        #expect(response.outcome == .sent)
        #expect(response.inputId == 42)
        #expect(response.queued.isEmpty)
    }

    @Test
    func sessionInputResponseDecodesQueuedOutcome() throws {
        let json = """
        {
          "outcome": "queued",
          "input_id": 7,
          "intent": "auto",
          "queued": [
            {
              "id": 7,
              "text": "hold this thought",
              "intent": "auto",
              "status": "queued",
              "last_error": null,
              "created_at": "2026-04-26T23:00:00Z"
            }
          ]
        }
        """.data(using: .utf8)!

        let response = try JSONDecoder.snakeCase.decode(SessionInputResponse.self, from: json)
        #expect(response.outcome == .queued)
        #expect(response.queued.count == 1)
        #expect(response.queued.first?.status == "queued")
        #expect(response.queued.first?.text == "hold this thought")
        #expect(response.pendingInputCount == 1)
        #expect(response.visibleFailedInputCount == 0)
    }

    @Test
    func sessionCapabilitiesDecodesSteerAndQueueFlags() throws {
        let json = """
        {
          "live_control_available": true,
          "host_reattach_available": true,
          "reply_to_live_session_available": true,
          "can_queue_next_input": true,
          "can_steer_active_turn": true,
          "display_label": "Live on this Mac",
          "display_detail": "Longhouse can send prompts into this live session.",
          "display_tone": "success"
        }
        """.data(using: .utf8)!

        let caps = try JSONDecoder.snakeCase.decode(SessionCapabilities.self, from: json)
        #expect(caps.canQueueNextInput == true)
        #expect(caps.canSteerActiveTurn == true)
    }

    @Test
    func sessionInputResponseSurfacesFailedRow() throws {
        let json = """
        {
          "outcome": "queued",
          "input_id": 9,
          "intent": "auto",
          "queued": [
            {
              "id": 9,
              "text": "retry me",
              "intent": "auto",
              "status": "failed",
              "last_error": "provider unavailable",
              "created_at": "2026-04-26T23:10:00Z"
            }
          ]
        }
        """.data(using: .utf8)!

        let response = try JSONDecoder.snakeCase.decode(SessionInputResponse.self, from: json)
        #expect(response.queued.first?.status == "failed")
        #expect(response.queued.first?.lastError == "provider unavailable")
        #expect(response.pendingInputCount == 0)
        #expect(response.visibleFailedInputCount == 1)
    }

    @Test
    func sessionInputResponseHidesTurnEndedSteerFailureFromFailedCount() throws {
        let json = """
        {
          "outcome": "queued",
          "input_id": 10,
          "intent": "steer",
          "queued": [
            {
              "id": 10,
              "text": "update this turn",
              "intent": "steer",
              "status": "failed",
              "last_error": "turn_ended",
              "created_at": "2026-04-26T23:12:00Z"
            }
          ]
        }
        """.data(using: .utf8)!

        let response = try JSONDecoder.snakeCase.decode(SessionInputResponse.self, from: json)
        #expect(response.pendingInputCount == 0)
        #expect(response.visibleFailedInputCount == 0)
    }
}
