import Foundation
import Testing

@testable import Longhouse

struct TranscriptSnapshotStoreTests {
    private func tempDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-snapshot-tests-\(UUID().uuidString)", isDirectory: true)
    }

    private func makeStore(
        directory: URL,
        ttl: TimeInterval = 14 * 24 * 60 * 60,
        maxFiles: Int = 40
    ) -> TranscriptSnapshotStore {
        TranscriptSnapshotStore(directory: directory, ttl: ttl, maxFiles: maxFiles)
    }

    private func makeDetail(id: String = "session-1") -> SessionDetail {
        let json = """
        {
          "id": "\(id)",
          "provider": "codex",
          "project": "zerg",
          "summary_title": "Snapshot Session",
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
        }
        """.data(using: .utf8)!
        return try! JSONDecoder.snakeCase.decode(SessionDetail.self, from: json)
    }

    private func makeEvent(id: Int, content: String) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "user",
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: "2026-05-02T20:00:00Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    @Test
    func roundTripsSnapshotThroughDisk() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = makeStore(directory: dir)
        let events = [makeEvent(id: 10, content: "Hello"), makeEvent(id: 11, content: "World")]

        store.save(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            detail: makeDetail(),
            events: events,
            loadedProjectionItemCount: 2,
            totalProjectionItemCount: 2,
            tailSnapshotEventId: 11,
            lastPubsubSeq: 42
        )
        store.waitForPendingWrites()

        let loaded = store.load(serverURL: "https://example.longhouse.ai", sessionId: "session-1")
        #expect(loaded != nil)
        #expect(loaded?.events.map(\.id) == [10, 11])
        #expect(loaded?.detail.id == "session-1")
        #expect(loaded?.tailSnapshotEventId == 11)
        #expect(loaded?.lastPubsubSeq == 42)
    }

    @Test
    func normalizesServerURLAcrossTrailingSlashAndCase() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = makeStore(directory: dir)

        store.save(
            serverURL: "https://Example.Longhouse.ai/",
            sessionId: "session-1",
            detail: makeDetail(),
            events: [makeEvent(id: 1, content: "hi")],
            loadedProjectionItemCount: 1,
            totalProjectionItemCount: 1,
            tailSnapshotEventId: 1,
            lastPubsubSeq: nil
        )
        store.waitForPendingWrites()

        let loaded = store.load(serverURL: "https://example.longhouse.ai", sessionId: "session-1")
        #expect(loaded != nil)
    }

    @Test
    func expiredSnapshotReturnsNilAndIsPruned() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = makeStore(directory: dir, ttl: 60)

        store.save(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            detail: makeDetail(),
            events: [makeEvent(id: 1, content: "hi")],
            loadedProjectionItemCount: 1,
            totalProjectionItemCount: 1,
            tailSnapshotEventId: 1,
            lastPubsubSeq: nil,
            savedAt: Date(timeIntervalSince1970: 1_000_000)
        )
        store.waitForPendingWrites()

        // Query "now" far past the TTL.
        let loaded = store.load(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            now: Date(timeIntervalSince1970: 1_000_000 + 3600)
        )
        #expect(loaded == nil)
    }

    @Test
    func clearByServerRemovesOnlyThatServer() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = makeStore(directory: dir)

        store.save(
            serverURL: "https://a.longhouse.ai",
            sessionId: "session-1",
            detail: makeDetail(),
            events: [makeEvent(id: 1, content: "a")],
            loadedProjectionItemCount: 1,
            totalProjectionItemCount: 1,
            tailSnapshotEventId: 1,
            lastPubsubSeq: nil
        )
        store.save(
            serverURL: "https://b.longhouse.ai",
            sessionId: "session-1",
            detail: makeDetail(),
            events: [makeEvent(id: 2, content: "b")],
            loadedProjectionItemCount: 1,
            totalProjectionItemCount: 1,
            tailSnapshotEventId: 2,
            lastPubsubSeq: nil
        )
        store.waitForPendingWrites()

        store.clear(serverURL: "https://a.longhouse.ai")
        store.waitForPendingWrites()

        #expect(store.load(serverURL: "https://a.longhouse.ai", sessionId: "session-1") == nil)
        #expect(store.load(serverURL: "https://b.longhouse.ai", sessionId: "session-1") != nil)
    }

    @Test
    func schemaMismatchIsIgnored() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = makeStore(directory: dir)

        store.save(
            serverURL: "https://example.longhouse.ai",
            sessionId: "session-1",
            detail: makeDetail(),
            events: [makeEvent(id: 1, content: "hi")],
            loadedProjectionItemCount: 1,
            totalProjectionItemCount: 1,
            tailSnapshotEventId: 1,
            lastPubsubSeq: nil
        )
        store.waitForPendingWrites()

        // Hand-write a file with a bumped schema version under the same key.
        let files = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
        let file = files.first { $0.pathExtension == "json" }!
        var json = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [String: Any]
        json["schemaVersion"] = TranscriptSnapshotStore.schemaVersion + 1
        try JSONSerialization.data(withJSONObject: json).write(to: file)

        #expect(store.load(serverURL: "https://example.longhouse.ai", sessionId: "session-1") == nil)
    }

    @Test
    func evictsOldestBeyondFileCap() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = makeStore(directory: dir, maxFiles: 2)

        for index in 0..<4 {
            store.save(
                serverURL: "https://example.longhouse.ai",
                sessionId: "session-\(index)",
                detail: makeDetail(id: "session-\(index)"),
                events: [makeEvent(id: index, content: "msg-\(index)")],
                loadedProjectionItemCount: 1,
                totalProjectionItemCount: 1,
                tailSnapshotEventId: index,
                lastPubsubSeq: nil
            )
            store.waitForPendingWrites()
        }

        let remaining = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        #expect(remaining.count <= 2)
    }
}
