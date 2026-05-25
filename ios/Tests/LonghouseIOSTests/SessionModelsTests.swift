import Foundation
import Testing
@testable import Longhouse

struct SessionModelsTests {
    private func runtimeFacts(
        controlPath: String = "managed",
        processState: String = "unknown",
        hostState: String = "unknown",
        processStatus: String = "unknown",
        phaseKind: String? = nil,
        phaseTool: String? = nil,
        phaseExpiresAt: String? = nil,
        transcriptAt: String? = nil,
        lifecycleState: String = "unknown",
        lifecycleReason: String? = nil,
        observedAt: String = "2026-04-25T20:00:00Z"
    ) -> SessionLivenessFacts {
        SessionLivenessFacts(
            controlPath: controlPath,
            processState: processState,
            host: HostObservation(
                state: hostState,
                lastSeenAt: hostState == "unknown" ? nil : observedAt,
                source: hostState == "unknown" ? nil : "machine_heartbeat"
            ),
            process: ProcessObservation(
                status: processStatus,
                pid: processStatus == "observed" ? 123 : nil,
                processStartTime: processStatus == "observed" ? "2026-04-25T19:00:00Z" : nil,
                observedAt: processStatus == "observed" ? observedAt : nil,
                lastSeenAt: processStatus == "unknown" ? nil : observedAt,
                sourceMtime: nil,
                sourcePath: processStatus == "observed" ? "/tmp/session.jsonl" : nil,
                reason: nil,
                source: processStatus == "unknown" ? nil : "machine_process_scan"
            ),
            phase: PhaseObservation(
                kind: phaseKind,
                tool: phaseTool,
                source: phaseKind == nil ? nil : "managed_local_transport",
                observedAt: phaseKind == nil ? nil : observedAt,
                expiresAt: phaseKind == nil ? nil : (phaseExpiresAt ?? "2026-04-25T20:15:00Z")
            ),
            activity: ActivityObservation(
                lastTranscriptAt: transcriptAt,
                lastRuntimeSignalAt: phaseKind == nil ? nil : observedAt,
                lastProgressAt: nil
            ),
            lifecycle: LifecycleFact(
                state: lifecycleState,
                reason: lifecycleReason,
                observedAt: lifecycleState == "unknown" ? nil : observedAt
            )
        )
    }

    private func runtimeDisplay(activityRecency: String?) -> SessionRuntimeDisplay {
        SessionRuntimeDisplay(
            truthTier: "managed-local",
            state: "running",
            tone: "running",
            headline: "Using Codex",
            detail: nil,
            phaseLabel: "Using Codex",
            compactToolLabel: "Codex",
            isLive: activityRecency == "live" || activityRecency == "recent",
            isExecuting: true,
            needsAttention: false,
            isIdle: false,
            isManagedLocalTruth: true,
            hasSignal: true,
            controlPath: "managed",
            activityRecency: activityRecency,
            lifecycle: "open",
            hostState: "online",
            terminalReason: nil
        )
    }

    private func apiSessionJSON(id: String = "session-card-contract") -> String {
        """
        {
          "id": "\(id)",
          "summary_title": "Timeline contract",
          "summary": "Backend emits card presentation.",
          "status": "idle",
          "presence_state": "needs_user",
          "presence_tool": null,
          "active_tool": null,
          "display_phase": "Idle",
          "user_state": "active",
          "provider": "claude",
          "project": "zerg",
          "git_branch": "main",
          "started_at": "2026-04-25T19:00:00Z",
          "ended_at": null,
          "home_label": "On this Mac",
          "timeline_anchor_at": "2026-04-25T20:00:00Z",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "user_messages": 3,
          "assistant_messages": 2,
          "tool_calls": 4,
          "thread_root_session_id": "\(id)",
          "thread_head_session_id": "\(id)",
          "thread_continuation_count": 0,
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true
          },
          "loop_mode": "assist",
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": "needs_user",
            "tone": "idle",
            "headline": "Idle",
            "detail": "Waiting for next prompt",
            "phase_label": "Idle",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": true,
            "is_stalled": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "runtime_facts": {
            "control_path": "managed",
            "control": {"state": "online", "reason": null, "source": "machine_heartbeat", "last_seen_at": "2026-04-25T20:00:00Z", "expires_at": "2026-04-25T20:15:00Z", "transport": "claude_channel_bridge"},
            "process_state": "unknown",
            "host": {"state": "online", "last_seen_at": "2026-04-25T20:00:00Z", "source": "machine_heartbeat"},
            "process": {"status": "unknown", "pid": null, "process_start_time": null, "observed_at": null, "last_seen_at": null, "source_mtime": null, "source_path": null, "reason": null, "source": null},
            "phase": {"kind": "needs_user", "tool": null, "source": "managed_local_transport", "observed_at": "2026-04-25T20:00:00Z", "expires_at": "2026-04-25T20:15:00Z"},
            "activity": {"last_transcript_at": "2026-04-25T20:00:00Z", "last_runtime_signal_at": "2026-04-25T20:00:00Z", "last_progress_at": null},
            "lifecycle": {"state": "open", "reason": "phase_observed", "observed_at": "2026-04-25T20:00:00Z"}
          },
          "timeline_card": {
            "ownership": {"label": "Managed", "tone": "neutral"},
            "status": {"label": "Idle", "tone": "idle", "seen_at": null, "seen_at_prefix": "Updated"},
            "border_tone": "idle"
          }
        }
        """
    }

    private func summaryForPhaseFreshness(
        activityRecency: String?,
        phaseExpiresAt: String? = nil,
        includeRuntimeFacts: Bool = true
    ) -> SessionSummary {
        SessionSummary(
            id: "freshness-test",
            title: "Freshness test",
            presenceState: "running",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "running",
            displayPhase: "Using Codex",
            runtimeDisplay: runtimeDisplay(activityRecency: activityRecency),
            runtimeFacts: includeRuntimeFacts
                ? runtimeFacts(
                    processState: "running",
                    hostState: "online",
                    processStatus: "observed",
                    phaseKind: "running",
                    phaseTool: "codex",
                    phaseExpiresAt: phaseExpiresAt
                )
                : nil
        )
    }

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
              "headline": "Idle",
              "detail": null,
              "phase_label": "Idle",
              "compact_tool_label": null,
              "is_live": false,
              "is_executing": false,
              "needs_attention": false,
              "is_idle": true,
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
    func sessionEventToolInputJSONPreservesOriginalKeys() throws {
        let json = """
        {
          "id": 42,
          "role": "assistant",
          "content_text": null,
          "tool_name": "edit",
          "tool_input_json": {
            "file_path": "/tmp/example.swift",
            "nested_value": {
              "inner_key": "kept"
            }
          },
          "tool_output_text": null,
          "tool_call_id": "call-1",
          "timestamp": "2026-05-15T20:00:00Z",
          "in_active_context": true,
          "is_head_branch": true
        }
        """.data(using: .utf8)!

        let event = try JSONDecoder.snakeCase.decode(SessionEvent.self, from: json)

        #expect(event.toolInputString("file_path") == "/tmp/example.swift")
        #expect(event.toolInputString("filePath") == nil)
        guard case let .object(nested)? = event.toolInputJSON?["nested_value"] else {
            Issue.record("expected nested_value object")
            return
        }
        #expect(nested["inner_key"] == .string("kept"))
        #expect(nested["innerKey"] == nil)
    }

    @Test
    func sessionEventDecodesInputOriginStates() throws {
        let longhouseJSON = """
        {
          "id": 43,
          "role": "user",
          "content_text": "sent from ios",
          "tool_name": null,
          "tool_input_json": null,
          "tool_output_text": null,
          "tool_call_id": null,
          "timestamp": "2026-05-02T20:00:00Z",
          "in_active_context": true,
          "is_head_branch": true,
          "input_origin": {
            "authored_via": "longhouse",
            "session_input_id": 7,
            "client_request_id": "ios-origin-1"
          }
        }
        """.data(using: .utf8)!
        let omittedJSON = """
        {
          "id": 44,
          "role": "assistant",
          "content_text": "ack",
          "tool_name": null,
          "tool_input_json": null,
          "tool_output_text": null,
          "tool_call_id": null,
          "timestamp": "2026-05-02T20:00:01Z",
          "in_active_context": true,
          "is_head_branch": true
        }
        """.data(using: .utf8)!
        let unknownJSON = """
        {
          "id": 45,
          "role": "user",
          "content_text": "future origin",
          "tool_name": null,
          "tool_input_json": null,
          "tool_output_text": null,
          "tool_call_id": null,
          "timestamp": "2026-05-02T20:00:02Z",
          "in_active_context": true,
          "is_head_branch": true,
          "input_origin": {
            "authored_via": "watch",
            "session_input_id": null,
            "client_request_id": null
          }
        }
        """.data(using: .utf8)!

        let longhouse = try JSONDecoder.snakeCase.decode(SessionEvent.self, from: longhouseJSON)
        let omitted = try JSONDecoder.snakeCase.decode(SessionEvent.self, from: omittedJSON)
        let unknown = try JSONDecoder.snakeCase.decode(SessionEvent.self, from: unknownJSON)

        #expect(longhouse.inputOrigin?.authoredVia == .longhouse)
        #expect(longhouse.inputOrigin?.sessionInputId == 7)
        #expect(longhouse.inputOrigin?.clientRequestId == "ios-origin-1")
        #expect(omitted.inputOrigin == nil)
        #expect(unknown.inputOrigin?.authoredVia == .unknown("watch"))
    }

    @Test
    func generatedAPIEventToolInputJSONPreservesOriginalKeys() throws {
        let json = """
        {
          "id": 43,
          "role": "assistant",
          "content_text": null,
          "tool_name": "edit",
          "tool_input_json": {
            "file_path": "/tmp/generated.swift"
          },
          "tool_output_text": null,
          "tool_call_id": "call-2",
          "timestamp": "2026-05-15T20:00:00Z",
          "in_active_context": true,
          "is_head_branch": true
        }
        """.data(using: .utf8)!

        let event = try JSONDecoder.snakeCase.decode(APIEventResponse.self, from: json)

        #expect(event.toolInputJson?["file_path"] == .string("/tmp/generated.swift"))
        #expect(event.toolInputJson?["filePath"] == nil)
    }

    @Test
    func apiSessionWorkspaceResponseAdaptsGeneratedDTOs() throws {
        let sessionJSON = apiSessionJSON(id: "workspace-session")
        let json = """
        {
          "session": \(sessionJSON),
          "thread": {
            "root_session_id": "workspace-session",
            "head_session_id": "workspace-session",
            "sessions": [\(sessionJSON)]
          },
          "projection": {
            "root_session_id": "workspace-session",
            "focus_session_id": "workspace-session",
            "head_session_id": "workspace-session",
            "path_session_ids": ["workspace-session"],
            "items": [
              {
                "kind": "event",
                "session_id": "workspace-session",
                "timestamp": "2026-05-15T20:00:00Z",
                "event": {
                  "id": 44,
                  "role": "assistant",
                  "content_text": null,
                  "tool_name": "edit",
                  "tool_input_json": {"file_path": "/tmp/workspace.swift"},
                  "tool_output_text": null,
                  "tool_call_id": "call-3",
                  "timestamp": "2026-05-15T20:00:00Z",
                  "in_active_context": true,
                  "is_head_branch": true
                }
              }
            ],
            "total": 1,
            "page_offset": 0,
            "branch_mode": "head",
            "abandoned_events": 0
          }
        }
        """.data(using: .utf8)!

        let apiWorkspace = try JSONDecoder.snakeCase.decode(APISessionWorkspaceResponse.self, from: json)
        let workspace = apiWorkspace.sessionWorkspaceResponse

        #expect(workspace.session.id == "workspace-session")
        #expect(workspace.thread.sessions.map(\.id) == ["workspace-session"])
        #expect(workspace.events.map(\.id) == [44])
        #expect(workspace.events.first?.toolInputString("file_path") == "/tmp/workspace.swift")
    }

    @Test
    func generatedControlResponsesAdaptToDomainModels() throws {
        let inputJSON = """
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
        let turnsJSON = """
        {
          "turns": [
            {
              "id": 99,
              "session_id": "session-1",
              "state": "terminal",
              "terminal_phase": "needs_user",
              "error_code": null,
              "user_submitted_at": "2026-04-26T23:10:00Z",
              "terminal_at": "2026-04-26T23:11:00Z",
              "timing": {}
            }
          ],
          "total": 1
        }
        """.data(using: .utf8)!
        let draftJSON = """
        {
          "draft_text": "Looks good.",
          "model": "codex",
          "generated_at": "2026-04-26T23:12:00Z",
          "based_on_event_ids": [1, 2]
        }
        """.data(using: .utf8)!
        let loopJSON = """
        {
          "session_id": "session-1",
          "loop_mode": "autopilot"
        }
        """.data(using: .utf8)!

        let input = try JSONDecoder.snakeCase.decode(APISessionInputResponse.self, from: inputJSON).sessionInputResponse
        let turns = try JSONDecoder.snakeCase.decode(APISessionTurnsListResponse.self, from: turnsJSON).sessionTurnsResponse
        let draft = try JSONDecoder.snakeCase.decode(APISessionDraftReplyResponse.self, from: draftJSON).draftReplyResponse
        let loop = try JSONDecoder.snakeCase.decode(APISessionLoopModeResponse.self, from: loopJSON).loopModeResponse

        #expect(input.outcome == .queued)
        #expect(input.visibleFailedInputCount == 0)
        #expect(turns.turns.first?.terminalPhase == "needs_user")
        #expect(draft.basedOnEventIds == [1, 2])
        #expect(loop.loopMode == .autopilot)
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
          "display_phase": "Idle",
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
          "runtime_display": {
            "truth_tier": "managed-local",
            "state": "needs_user",
            "tone": "idle",
            "headline": "Idle",
            "detail": null,
            "phase_label": "Idle",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": true,
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

        #expect(detail.effectiveLoopMode == .assist)
        #expect(detail.canSendLive)
        #expect(detail.runtimeCapabilityLabel == "Live on this Mac")
        #expect(detail.runtimeCapabilityTone == "success")
        #expect(detail.runtimePhaseLabel == "Idle")
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
            "detail": "Using Shell",
            "phase_label": "Using Shell",
            "compact_tool_label": "Shell",
            "is_live": true,
            "is_executing": true,
            "needs_attention": false,
            "is_idle": false,
            "is_managed_local_truth": true,
            "has_signal": true
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimeHeadline == "Working")
        #expect(detail.runtimeDetail == "Using Shell")
        #expect(detail.runtimeCapabilityLabel == "Live on this Mac")
        #expect(detail.runtimePhaseLabel == "Using Shell")
        #expect(detail.runtimeTone == "running")
        #expect(detail.isSessionExecuting)
    }

    @Test
    func sessionDetailUsesTranscriptSyncDisplayOverIdleFacts() throws {
        let json = """
        {
          "id": "session-syncing",
          "provider": "claude",
          "project": "zerg",
          "presence_state": "idle",
          "user_state": "active",
          "status": "idle",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "display_phase": "Idle",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": "syncing_transcript",
            "tone": "active",
            "headline": "Syncing",
            "detail": "Waiting for transcript",
            "phase_label": "Syncing transcript",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "runtime_facts": {
            "control_path": "managed",
            "host": {"state": "online", "last_seen_at": "2026-04-25T20:00:00Z", "source": "machine_heartbeat"},
            "process": {"status": "unknown", "pid": null, "process_start_time": null, "observed_at": null, "last_seen_at": null, "source_mtime": null, "source_path": null, "reason": null, "source": null},
            "phase": {"kind": "idle", "tool": null, "source": "claude_hook", "observed_at": "2026-04-25T20:00:01Z", "expires_at": "2026-04-25T20:10:01Z"},
            "activity": {"last_transcript_at": "2026-04-25T20:00:00Z", "last_runtime_signal_at": "2026-04-25T20:00:01Z", "last_progress_at": null},
            "lifecycle": {"state": "open", "reason": "phase_observed", "observed_at": "2026-04-25T20:00:01Z"}
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimePhaseState == "syncing_transcript")
        #expect(detail.runtimePhaseLabel == "Syncing transcript")
        #expect(detail.runtimeHeadline == "Syncing")
        #expect(detail.runtimeDetail == "Waiting for transcript")
        #expect(detail.runtimeTone == "active")
        #expect(!detail.isSessionExecuting)
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
    func sessionDetailCanonicalizesRuntimeDisplayShellLabels() throws {
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
          "runtime_display": {
            "truth_tier": "managed-local",
            "state": "running",
            "tone": "running",
            "headline": "Working",
            "detail": "running bash",
            "phase_label": "running bash",
            "compact_tool_label": "bash",
            "is_live": true,
            "is_executing": true,
            "needs_attention": false,
            "is_idle": false,
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

        #expect(detail.runtimePhaseLabel == "Using Shell")
        #expect(detail.runtimeDetail == "Using Shell")
    }

    @Test
    func sessionDetailPrefersRuntimeDisplayOverRuntimeFacts() throws {
        let jsonString = """
        {
          "id": "session-facts-detail",
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
            "detail": "Using Shell",
            "phase_label": "Using Shell",
            "compact_tool_label": "Shell",
            "is_live": true,
            "is_executing": true,
            "needs_attention": false,
            "is_idle": false,
            "is_managed_local_truth": true,
            "has_signal": true
          },
          "runtime_facts": {
            "control_path": "managed",
            "host": {"state": "online", "last_seen_at": "2026-04-25T20:00:00Z", "source": "machine_heartbeat"},
            "process": {"status": "unknown", "pid": null, "process_start_time": null, "observed_at": null, "last_seen_at": null, "source_mtime": null, "source_path": null, "reason": null, "source": null},
            "phase": {"kind": "running", "tool": "shell", "source": "managed_local_transport", "observed_at": "2026-04-25T20:00:00Z", "expires_at": "2026-04-25T20:15:00Z"},
            "activity": {"last_transcript_at": "2026-04-25T20:00:00Z", "last_runtime_signal_at": "2026-04-25T20:00:00Z", "last_progress_at": null},
            "lifecycle": {"state": "open", "reason": "phase_observed", "observed_at": "2026-04-25T20:00:00Z"}
          },
          "loop_mode": "assist"
        }
        """

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: Data(jsonString.utf8))

        #expect(detail.runtimePhaseLabel == "Using Shell")
        #expect(detail.runtimeHeadline == "Working")
        #expect(detail.runtimeDetail == "Using Shell")
        #expect(detail.runtimeTone == "running")
        #expect(detail.isSessionExecuting)

        let noPhaseJson = jsonString.replacingOccurrences(
            of: #""phase": {"kind": "running", "tool": "shell", "source": "managed_local_transport", "observed_at": "2026-04-25T20:00:00Z", "expires_at": "2026-04-25T20:15:00Z"}"#,
            with: #""phase": {"kind": null, "tool": null, "source": null, "observed_at": null, "expires_at": null}"#
        )
        let noPhaseDetail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: Data(noPhaseJson.utf8))

        #expect(noPhaseDetail.runtimePhaseState == "running")
        #expect(noPhaseDetail.runtimePhaseLabel == "Using Shell")
        #expect(noPhaseDetail.runtimeHeadline == "Working")
        #expect(noPhaseDetail.isSessionExecuting)
    }

    @Test
    func runtimeDisplayTextCanonicalizesOnlyBareShellAliases() {
        #expect(RuntimeDisplayText.canonicalDisplayText("Running bash") == "Using Shell")
        #expect(RuntimeDisplayText.canonicalDisplayText("Blocked on terminal") == "Blocked on Shell")
        #expect(RuntimeDisplayText.canonicalDisplayText("Approval needed \u{2022} shell") == "Approval needed \u{2022} Shell")
        #expect(RuntimeDisplayText.canonicalDisplayText("Running bash-runner") == "Running bash-runner")
        #expect(RuntimeDisplayText.canonicalDisplayText("Running bash script") == "Running bash script")
    }

    @Test
    func sessionSummaryCanonicalizesRuntimeDisplayShellLabels() {
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
            activeTool: "bash",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "running",
                tone: "running",
                headline: "Working",
                detail: "running bash",
                phaseLabel: "Running bash",
                compactToolLabel: "bash",
                isLive: true,
                isExecuting: true,
                needsAttention: false,
                isIdle: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            )
        )

        #expect(summary.displayPhaseLabel == "Using Shell")
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
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Idle",
                detail: "Waiting for next prompt",
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: true,
                isIdle: false,
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
        #expect(summary.displayPhaseLabel == "Closed")
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
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: nil,
                tone: "inactive",
                headline: "Not connected",
                detail: nil,
                phaseLabel: "Inactive",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: false,
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
        #expect(summary.displayPhaseLabel == "Inactive")
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
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "fresh",
                state: "needs_user",
                tone: "idle",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isManagedLocalTruth: false,
                hasSignal: true,
                controlPath: "unmanaged",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "unknown",
                terminalReason: nil
            )
        )

        #expect(stale.timelineStatusLabel == "No live signal")
        #expect(stale.timelineStatusSeenAt == nil)
        #expect(live.timelineStatusLabel == "No live signal")
    }

    @Test
    func timelineStatusKeepsManagedIdleSeparateFromUnmanagedActivity() {
        let summary = SessionSummary(
            id: "session-managed-idle",
            title: "Managed session",
            presenceState: "needs_user",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            )
        )

        #expect(summary.timelineStatusLabel == "No live signal")
    }

    @Test
    func phaseSignalFreshUsesRuntimeFactDeadlineWhenPresent() {
        let future = ISO8601DateFormatter().string(from: Date().addingTimeInterval(60))
        let past = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-60))

        #expect(phaseSignalFresh(summaryForPhaseFreshness(activityRecency: "stale", phaseExpiresAt: future)))
        #expect(!phaseSignalFresh(summaryForPhaseFreshness(activityRecency: "live", phaseExpiresAt: past)))
    }

    @Test
    func phaseSignalFreshRequiresRuntimeFactDeadlineForLegacyPayloads() {
        #expect(!phaseSignalFresh(summaryForPhaseFreshness(activityRecency: "live", includeRuntimeFacts: false)))
        #expect(!phaseSignalFresh(summaryForPhaseFreshness(activityRecency: "recent", includeRuntimeFacts: false)))
        #expect(!phaseSignalFresh(summaryForPhaseFreshness(activityRecency: "stale", includeRuntimeFacts: false)))
        #expect(!phaseSignalFresh(summaryForPhaseFreshness(activityRecency: nil, includeRuntimeFacts: false)))
    }

    @Test
    func timelineCardPresentationOverridesLocalRuntimeDerivation() {
        let summary = SessionSummary(
            id: "session-backend-card",
            title: "Backend-owned card",
            presenceState: "needs_user",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Idle",
                detail: nil,
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            ),
            timelineCard: TimelineCardPresentation(
                ownership: TimelineBadgePresentation(label: "Unmanaged", tone: "neutral"),
                status: TimelineStatusPresentation(label: "Stale", tone: "inactive", seenAt: "2026-04-25T19:00:00Z", seenAtPrefix: "Updated"),
                borderTone: "inactive"
            )
        )

        #expect(summary.managementLabel == "Unmanaged")
        #expect(summary.timelineStatusLabel == "Stale")
        #expect(summary.timelineStatusSeenAt == "2026-04-25T19:00:00Z")
        #expect(summary.timelineStatusTone == "inactive")
        #expect(summary.timelineBorderTone == "inactive")
    }

    @Test
    func runtimeDisplayOverridesRuntimeFactsPresentation() {
        let managedPhase = SessionSummary(
            id: "session-fact-phase",
            title: "Managed phase",
            presenceState: "running",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "working",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "running",
                tone: "running",
                headline: "Working",
                detail: "Using Shell",
                phaseLabel: "Using Shell",
                compactToolLabel: "Shell",
                isLive: true,
                isExecuting: true,
                needsAttention: false,
                isIdle: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            ),
            runtimeFacts: runtimeFacts(
                controlPath: "managed",
                hostState: "online",
                phaseKind: "running",
                phaseTool: "mcp__hatch__hatch_codex",
                transcriptAt: "2026-04-25T20:00:00Z",
                lifecycleState: "open",
                lifecycleReason: "phase_observed"
            )
        )

        #expect(managedPhase.managementLabel == "Managed")
        #expect(managedPhase.timelineStatusLabel == "No live signal")
        #expect(managedPhase.displayPhaseLabel == "Using Shell")
        #expect(managedPhase.timelineStatusTone == "inactive")
        #expect(managedPhase.isExecuting)
    }

    @Test
    func runtimeFactsRenderUnmanagedProcessTranscriptHostAndClosedStates() {
        let processObserved = SessionSummary(
            id: "session-process-observed",
            title: "Unmanaged process",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "working",
            runtimeFacts: runtimeFacts(
                controlPath: "unmanaged",
                processState: "running",
                hostState: "online",
                processStatus: "observed",
                transcriptAt: "2026-04-25T20:00:00Z",
                lifecycleState: "open",
                lifecycleReason: "process_observed"
            )
        )
        let transcriptOnly = SessionSummary(
            id: "session-transcript-only",
            title: "Transcript only",
            presenceState: "idle",
            provider: "claude",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "active",
            runtimeFacts: runtimeFacts(controlPath: "unmanaged", transcriptAt: "2026-04-25T20:00:00Z")
        )
        let hostUnverified = SessionSummary(
            id: "session-host-unverified",
            title: "Runtime unverified",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "idle",
            runtimeFacts: runtimeFacts(controlPath: "managed")
        )
        let closed = SessionSummary(
            id: "session-closed-fact",
            title: "Closed",
            presenceState: "running",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "working",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "stale",
                state: nil,
                tone: "closed",
                headline: "Closed",
                detail: nil,
                phaseLabel: "Closed",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isManagedLocalTruth: false,
                hasSignal: true,
                controlPath: "unmanaged",
                activityRecency: "stale",
                lifecycle: "closed",
                hostState: "unknown",
                terminalReason: "provider_signal"
            ),
            runtimeFacts: runtimeFacts(
                controlPath: "unmanaged",
                transcriptAt: "2026-04-25T20:00:00Z",
                lifecycleState: "closed",
                lifecycleReason: "session_ended"
            )
        )
        let terminalDisconnected = SessionSummary(
            id: "session-terminal-disconnected",
            title: "Terminal disconnected",
            presenceState: "needs_user",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "working",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Terminal disconnected",
                detail: "The terminal client disconnected.",
                phaseLabel: "Terminal disconnected",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "stale",
                lifecycle: "closed",
                hostState: "online",
                terminalReason: "terminal_disconnected"
            ),
            runtimeFacts: runtimeFacts(
                controlPath: "managed",
                processState: "closed",
                transcriptAt: "2026-04-25T20:00:00Z",
                lifecycleState: "closed",
                lifecycleReason: "terminal_disconnected"
            )
        )
        let unknownWithClosedLegacy = SessionSummary(
            id: "session-unknown-fact",
            title: "Unknown fact",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "completed",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "stale",
                state: nil,
                tone: "inactive",
                headline: "Closed",
                detail: nil,
                phaseLabel: "Closed",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: true,
                isManagedLocalTruth: false,
                hasSignal: true,
                controlPath: "unmanaged",
                activityRecency: "stale",
                lifecycle: "closed",
                hostState: "unknown",
                terminalReason: "process_gone"
            ),
            runtimeFacts: runtimeFacts(controlPath: "unmanaged", transcriptAt: "2026-04-25T20:00:00Z")
        )

        #expect(processObserved.managementLabel == "Unmanaged")
        #expect(processObserved.timelineStatusLabel == "No live signal")
        #expect(processObserved.timelineStatusTone == "inactive")
        #expect(processObserved.timelineStatusSeenAtPrefix == "Checked")
        #expect(transcriptOnly.timelineStatusLabel == "No live signal")
        #expect(transcriptOnly.timelineStatusSeenAtPrefix == "Checked")
        #expect(hostUnverified.timelineStatusLabel == "No live signal")
        #expect(closed.isClosed)
        #expect(closed.timelineStatusLabel == "No live signal")
        #expect(closed.timelineStatusTone == "inactive")
        #expect(closed.displayPhaseLabel == "Closed")
        #expect(!closed.isExecuting)
        #expect(terminalDisconnected.isClosed)
        #expect(terminalDisconnected.timelineStatusLabel == "No live signal")
        #expect(terminalDisconnected.timelineStatusTone == "inactive")
        #expect(terminalDisconnected.displayPhaseLabel == "Closed")
        #expect(!terminalDisconnected.isExecuting)
        #expect(unknownWithClosedLegacy.isClosed)
        #expect(unknownWithClosedLegacy.timelineStatusLabel == "No live signal")
    }

    @Test
    func apiTimelineSessionsListResponseDecodesTimelineCardContract() throws {
        let sessionJSON = """
        {
          "id": "session-card-contract",
          "summary_title": "Timeline contract",
          "summary": "Backend emits card presentation.",
          "status": "idle",
          "presence_state": "needs_user",
          "presence_tool": null,
          "active_tool": null,
          "display_phase": "Idle",
          "user_state": "active",
          "provider": "claude",
          "project": "zerg",
          "git_branch": "main",
          "started_at": "2026-04-25T19:00:00Z",
          "ended_at": null,
          "home_label": "On this Mac",
          "timeline_anchor_at": "2026-04-25T20:00:00Z",
          "last_activity_at": "2026-04-25T20:00:00Z",
          "user_messages": 3,
          "assistant_messages": 2,
          "tool_calls": 4,
          "thread_root_session_id": "session-card-contract",
          "thread_head_session_id": "session-card-contract",
          "thread_continuation_count": 0,
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true
          },
          "loop_mode": "assist",
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "phase_signal",
            "state": "needs_user",
            "tone": "idle",
            "headline": "Idle",
            "detail": "Waiting for next prompt",
            "phase_label": "Idle",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": true,
            "is_stalled": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "runtime_facts": {
            "control_path": "managed",
            "control": {"state": "online", "reason": null, "source": "machine_heartbeat", "last_seen_at": "2026-04-25T20:00:00Z", "expires_at": "2026-04-25T20:15:00Z", "transport": "claude_channel_bridge"},
            "process_state": "unknown",
            "host": {"state": "online", "last_seen_at": "2026-04-25T20:00:00Z", "source": "machine_heartbeat"},
            "process": {"status": "unknown", "pid": null, "process_start_time": null, "observed_at": null, "last_seen_at": null, "source_mtime": null, "source_path": null, "reason": null, "source": null},
            "phase": {"kind": "needs_user", "tool": null, "source": "managed_local_transport", "observed_at": "2026-04-25T20:00:00Z", "expires_at": "2026-04-25T20:15:00Z"},
            "activity": {"last_transcript_at": "2026-04-25T20:00:00Z", "last_runtime_signal_at": "2026-04-25T20:00:00Z", "last_progress_at": null},
            "lifecycle": {"state": "open", "reason": "phase_observed", "observed_at": "2026-04-25T20:00:00Z"}
          },
          "timeline_card": {
            "ownership": {"label": "Managed", "tone": "neutral"},
            "status": {"label": "Idle", "tone": "idle", "seen_at": null, "seen_at_prefix": "Updated"},
            "border_tone": "idle"
          }
        }
        """
        let json = """
        {
          "sessions": [
            {
              "thread_id": "session-card-contract",
              "timeline_anchor_at": "2026-04-25T20:05:00Z",
              "head_origin_label": "On this Mac",
              "head": \(sessionJSON),
              "detail": \(sessionJSON),
              "root": \(sessionJSON),
              "continuation_count": 0
            }
          ],
          "total": 1,
          "has_real_sessions": true
        }
        """

        let decoded = try JSONDecoder.snakeCase.decode(APITimelineSessionsListResponse.self, from: Data(json.utf8))
        let card = try #require(decoded.sessions.first)
        let session = card.head
        let summary = card.sessionSummary

        #expect(card.timelineAnchorAt == "2026-04-25T20:05:00Z")
        #expect(session.timelineAnchorAt == "2026-04-25T20:00:00Z")
        #expect(summary.timelineAnchorAt == "2026-04-25T20:05:00Z")
        #expect(session.timelineCard.ownership.label == "Managed")
        #expect(session.timelineCard.status.label == "Idle")
        #expect(session.timelineCard.borderTone == "idle")
        #expect(session.runtimeFacts?.controlPath == "managed")
        #expect(session.runtimeFacts?.control?.state == "online")
        #expect(session.runtimeFacts?.control?.source == "machine_heartbeat")
        #expect(session.runtimeFacts?.processState == "unknown")
        #expect(session.runtimeFacts?.phase.kind == "needs_user")
        #expect(summary.timelineCard?.ownership.label == "Managed")

        let pendingSessionJSON = sessionJSON
            .replacingOccurrences(of: #""summary_title": "Timeline contract","#, with: #""summary_title": null,"#)
            .replacingOccurrences(
                of: #""summary": "Backend emits card presentation.","#,
                with: #"""
          "summary": null,
          "summary_status": "pending",
          "first_user_message": "Investigate stuck generated summary cards.",
          """#
            )
        let pendingJSON = """
        {
          "sessions": [
            {
              "thread_id": "session-card-contract",
              "timeline_anchor_at": "2026-04-25T20:05:00Z",
              "head_origin_label": "On this Mac",
              "head": \(pendingSessionJSON),
              "detail": \(pendingSessionJSON),
              "root": \(pendingSessionJSON),
              "continuation_count": 0
            }
          ],
          "total": 1,
          "has_real_sessions": true
        }
        """
        let pendingDecoded = try JSONDecoder.snakeCase.decode(APITimelineSessionsListResponse.self, from: Data(pendingJSON.utf8))
        let pendingSummary = try #require(pendingDecoded.sessions.first?.sessionSummary)

        #expect(pendingSummary.title == "Investigate stuck generated summary cards.")
        #expect(pendingSummary.summaryStatusValue == .pending)
        #expect(pendingSummary.timelineSummaryPreview == nil)
    }

    @Test
    func sessionSummaryUsesTimelineCardStatusAndRuntimeDisplayPhase() {
        let summary = SessionSummary(
            id: "session-card-first",
            title: "Timeline card first",
            presenceState: "running",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-04-25T20:00:00Z",
            status: "working",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "running",
                tone: "running",
                headline: "Working",
                detail: "Using Shell",
                phaseLabel: "Using Shell",
                compactToolLabel: "Shell",
                isLive: true,
                isExecuting: true,
                needsAttention: false,
                isIdle: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: "live",
                lifecycle: "open",
                hostState: "online",
                terminalReason: nil
            ),
            runtimeFacts: runtimeFacts(
                controlPath: "managed",
                hostState: "online",
                phaseKind: "running",
                phaseTool: "shell",
                transcriptAt: "2026-04-25T20:00:00Z",
                lifecycleState: "open",
                lifecycleReason: "phase_observed"
            ),
            timelineCard: TimelineCardPresentation(
                ownership: TimelineBadgePresentation(label: "Managed", tone: "neutral"),
                status: TimelineStatusPresentation(label: "Idle", tone: "idle", seenAt: nil, seenAtPrefix: "Updated"),
                borderTone: "idle"
            )
        )

        #expect(summary.timelineStatusLabel == "Idle")
        #expect(summary.timelineStatusTone == "idle")
        #expect(summary.timelineBorderTone == "idle")
        #expect(summary.displayPhaseLabel == "Using Shell")
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
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "stale",
                state: "needs_user",
                tone: "idle",
                headline: "Inactive",
                detail: nil,
                phaseLabel: "Inactive",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: false,
                isIdle: false,
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
            displayPhase: "Idle",
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "managed-local",
                state: "needs_user",
                tone: "idle",
                headline: "Idle",
                detail: "Waiting for next prompt",
                phaseLabel: "Idle",
                compactToolLabel: nil,
                isLive: false,
                isExecuting: false,
                needsAttention: true,
                isIdle: false,
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
            displayPhase: "Idle"
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
        #expect(detail.runtimePhaseLabel == "Inactive")
    }

    @Test
    func sessionDetailPrefersServerReadOnlyInputModeOverReattachFallback() throws {
        let json = """
        {
          "id": "session-live-no-send",
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
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": false,
            "input_mode": "read_only",
            "default_input_intent": "none",
            "composer_enabled": false,
            "composer_disabled_reason": "This live Codex session is connected, but this control path cannot accept typed input.",
            "send_disabled_reason": "input_not_supported"
          },
          "loop_mode": "assist"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(!detail.canSendLive)
        #expect(!detail.isControlOffline)
        #expect(detail.isReadOnly)
        #expect(detail.runtimeCapabilityLabel == "Read only")
        #expect(detail.runtimeCapabilityTone == "neutral")
        #expect(detail.runtimeHeadline == "Read only")
        #expect(detail.controlHealthMessage == "This live Codex session is connected, but this control path cannot accept typed input.")
    }

    @Test
    func sessionDetailNormalizesLegacyCapabilityLabels() throws {
        let liveJSON = """
        {
          "id": "session-live-unmanaged",
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
          "id": "session-readonly-unmanaged",
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
          "runtime_display": {
            "truth_tier": "managed-local",
            "state": "needs_user",
            "tone": "idle",
            "headline": "Idle",
            "detail": null,
            "phase_label": "Idle",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": true,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "online",
            "terminal_reason": null
          },
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.runtimeHeadline == "Idle")
        #expect(detail.runtimeDetail == nil)
        #expect(detail.runtimePhaseLabel == "Idle")
    }

    @Test
    func sessionDetailUsesServerOwnedComposerSemantics() throws {
        let json = """
        {
          "id": "session-composer",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Composer session",
          "summary_title": "Composer session",
          "presence_state": "running",
          "presence_tool": null,
          "user_state": "active",
          "status": "running",
          "last_activity_at": null,
          "display_phase": null,
          "active_tool": null,
          "home_label": "On this Mac",
          "origin_label": "On this Mac",
          "capabilities": {
            "live_control_available": true,
            "host_reattach_available": true,
            "reply_to_live_session_available": true,
            "can_queue_next_input": true,
            "can_steer_active_turn": true,
            "display_label": "Live on this Mac",
            "display_detail": "Longhouse can send prompts into this live session.",
            "display_tone": "success",
            "input_mode": "live",
            "default_input_intent": "steer",
            "composer_enabled": true,
            "composer_placeholder": "Send a message to the live Codex session...",
            "composer_disabled_reason": null
          },
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(detail.canSendLive)
        #expect(detail.defaultInputIntent == "steer")
        #expect(detail.composerPlaceholder == "Send a message to the live Codex session...")
        #expect(detail.controlHealthMessage == nil)
    }

    @Test
    func closedComposer() throws {
        let json = """
        {
          "id": "closed-stale-live",
          "provider": "codex",
          "project": "zerg",
          "cwd": "/Users/davidrose/git/zerg",
          "git_branch": "main",
          "summary": "Closed session",
          "summary_title": "Closed session",
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
            "can_queue_next_input": true,
            "can_steer_active_turn": true,
            "display_label": "Closed",
            "display_detail": "This session has ended.",
            "display_tone": "neutral",
            "input_mode": "read_only",
            "default_input_intent": "none",
            "composer_enabled": true,
            "composer_placeholder": "Send a message to the live Codex session...",
            "composer_disabled_reason": "This session has ended."
          },
          "runtime_display": {
            "truth_tier": "managed-local",
            "signal_tier": "terminal_signal",
            "state": "finished",
            "tone": "neutral",
            "headline": "Closed",
            "detail": "This session has ended.",
            "phase_label": "Closed",
            "compact_tool_label": null,
            "is_live": false,
            "is_executing": false,
            "needs_attention": false,
            "is_idle": false,
            "is_managed_local_truth": true,
            "has_signal": true,
            "control_path": "managed",
            "activity_recency": "stale",
            "lifecycle": "closed",
            "host_state": "online",
            "terminal_reason": "terminal_disconnected"
          },
          "runtime_facts": {
            "control_path": "managed",
            "host": {"state": "online", "last_seen_at": "2026-05-24T15:03:10Z", "source": "machine_heartbeat"},
            "process": {"status": "unknown", "pid": null, "process_start_time": null, "observed_at": null, "last_seen_at": null, "source_mtime": null, "source_path": null, "reason": null, "source": null},
            "phase": {"kind": "finished", "tool": null, "source": "codex_bridge", "observed_at": "2026-05-24T15:05:10Z", "expires_at": null},
            "activity": {"last_transcript_at": null, "last_runtime_signal_at": "2026-05-24T15:05:10Z", "last_progress_at": null},
            "lifecycle": {"state": "closed", "reason": "terminal_disconnected", "observed_at": "2026-05-24T15:05:10Z"}
          },
          "loop_mode": "manual"
        }
        """.data(using: .utf8)!

        let detail = try JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)

        #expect(!detail.canSendLive)
        #expect(detail.attachImagesEnabled == false)
        #expect(detail.controlHealthMessage == "This session has ended.")
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
        #expect(response.queued.first?.status == .queued)
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
        #expect(response.queued.first?.status == .failed)
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
