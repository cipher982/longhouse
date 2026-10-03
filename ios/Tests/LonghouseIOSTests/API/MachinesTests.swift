import Foundation
import Testing
@testable import Longhouse

@Suite(.serialized)
struct MachinesTests {
    @Test
    func contractSummaryDecodesAndDefaultsOptionalFields() throws {
        let data = try #require("""
        {
          "generated_at": "2026-10-03T16:40:00Z",
          "days": 14,
          "utc_offset_minutes": -300,
          "first_day": "2026-09-20",
          "last_day": "2026-10-03",
          "machines": [{
            "machine": {
              "device_id": "cinder",
              "machine_name": "cinder",
              "online": true,
              "control_channel_status": "connected",
              "supports": ["omp.sign_in"],
              "control_operations_by_provider": {},
              "last_seen_at": "2026-10-03T16:40:00Z",
              "engine_build": null,
              "launch": {
                "blocked_by": null,
                "providers": [{"provider": "omp"}],
                "default_provider": "omp",
                "unavailable_providers": []
              }
            },
            "activity": {
              "sessions_started": 254,
              "daily": [{"date": "2026-09-20", "total": 45, "by_provider": {"omp": 43, "cursor": 1, "opencode": 1}}],
              "top_projects": [{"project": "zerg", "sessions": 78}],
              "latest_session": null,
              "live_count": 9,
              "live_sessions": [{"session_id": "abc", "title": "Working", "project": "zerg", "provider": "omp", "last_activity_at": null, "activity_state": "working"}]
            },
            "sync": {
              "reported_at": "2026-10-03T16:40:00Z",
              "report_age_seconds": 8,
              "stale": false,
              "status": "healthy",
              "status_summary": "All imported",
              "engine_version": null,
              "last_upload_at": "2026-10-03T16:39:52Z",
              "upload_p95_ms": 42,
              "waiting_uploads": 0,
              "failed_uploads": 0,
              "history": {"state": "current", "source_count": 3, "remaining_bytes": 0, "remaining_records": 0, "acknowledged_records": 20}
            }
          }]
        }
        """.data(using: .utf8))

        let response = try JSONDecoder.snakeCase.decode(MachinesSummaryResponse.self, from: data)
        #expect(response.machines.count == 1)
        #expect(response.machines[0].machine.machineName == "cinder")
        #expect(response.machines[0].activity.liveCount == 9)
        #expect(response.machines[0].activity.daily[0].byProvider["omp"] == 43)
        #expect(response.machines[0].sync?.history.state == "current")

        let sparse = try #require("{\"machines\":[{\"machine\":{\"device_id\":\"old\",\"machine_name\":\"old\",\"online\":false,\"launch\":{\"providers\":[]}}}]}".data(using: .utf8))
        let sparseResponse = try JSONDecoder.snakeCase.decode(MachinesSummaryResponse.self, from: sparse)
        #expect(sparseResponse.days == 14)
        #expect(sparseResponse.machines[0].activity.liveSessions.isEmpty)
        #expect(sparseResponse.machines[0].sync == nil)
    }

    @Test
    func summaryURLUsesTimelineRouteAndLocalOffset() throws {
        let baseURL = try #require(URL(string: "https://demo.longhouse.ai"))
        let components = try #require(URLComponents(
            url: LonghouseAPI.machineSummariesURL(baseURL: baseURL, days: 14, utcOffsetMinutes: -300),
            resolvingAgainstBaseURL: false
        ))
        #expect(components.path == "/api/timeline/machines/summary")
        #expect(components.queryItems == [
            URLQueryItem(name: "days", value: "14"),
            URLQueryItem(name: "utc_offset_minutes", value: "-300"),
        ])
    }

    @Test
    func statusDerivationFollowsContractTable() {
        let now = Date(timeIntervalSince1970: 1_791_043_200)
        let cases: [(MachineDirectoryEntry, MachineActivity?, MachineSync?, String, MachineStatusRole)] = [
            (machine(online: true), MachineActivity(liveCount: 9), nil, "9 live", .live),
            (machine(online: true), MachineActivity(liveCount: 9), MachineSync(stale: false, status: "broken"), "Needs repair", .fault),
            (machine(online: true, unavailable: [unavailable("codex", reason: "not_authenticated")]), nil, nil, "Codex signed out", .attention),
            (machine(online: true, unavailable: [unavailable("codex", reason: "cli_missing")]), nil, nil, "Codex not installed", .attention),
            (machine(online: true, blockedBy: "auth_failed"), nil, nil, "Needs repair", .fault),
            (machine(online: true, blockedBy: "engine_too_old"), nil, nil, "Update required", .attention),
            (machine(online: true, blockedBy: "no_launch_support"), nil, nil, "Can't start sessions", .attention),
            (machine(online: true), nil, nil, "Online", .live),
            (machine(online: false, blockedBy: "control_down"), nil, MachineSync(stale: false), "Sync only", .quiet),
            (machine(online: true), nil, MachineSync(status: "broken"), "Needs repair", .fault),
            (machine(online: false, lastSeenAt: "2026-09-25T16:40:00Z"), nil, nil, "Offline", .off),
        ]

        for (machine, activity, sync, text, role) in cases {
            let status = deriveMachineStatus(machine: machine, activity: activity, sync: sync, now: now)
            #expect(status.text == text)
            #expect(status.role == role)
        }
    }

    @Test
    func statusKeepsLastSeenSeparateFromRepairAction() {
        let now = Date(timeIntervalSince1970: 1_791_043_200)
        let offline = deriveMachineStatus(
            machine: machine(online: false, lastSeenAt: "2026-09-25T16:40:00Z"),
            now: now
        )
        #expect(offline.detail?.hasPrefix("last seen ") == true)

        let repair = deriveMachineStatus(
            machine: machine(online: false, blockedBy: "runtime_unreachable"),
            now: now
        )
        #expect(repair.detail == "Run longhouse local-health on this machine to inspect the fault")
    }

    @Test
    func directoryOnlyOnlineWithoutLaunchMetadataDoesNotReadAsIdle() {
        let status = deriveMachineStatus(machine: machine(online: true))
        #expect(status.text == "Online")
        #expect(status.detail == nil)
    }

    private func machine(
        online: Bool,
        lastSeenAt: String? = "2026-10-03T16:40:00Z",
        blockedBy: String? = nil,
        unavailable: [MachineLaunchUnavailableProvider] = []
    ) -> MachineDirectoryEntry {
        MachineDirectoryEntry(
            deviceId: UUID().uuidString,
            machineName: "fixture",
            online: online,
            controlChannelStatus: online ? "connected" : "control_down",
            supports: [],
            lastSeenAt: lastSeenAt,
            engineBuild: nil,
            launch: MachineLaunchProjection(
                blockedBy: blockedBy,
                providers: [],
                defaultProvider: nil,
                unavailableProviders: unavailable
            )
        )
    }

    private func unavailable(_ provider: String, reason: String) -> MachineLaunchUnavailableProvider {
        MachineLaunchUnavailableProvider(provider: provider, reason: reason, remediation: nil)
    }
}
