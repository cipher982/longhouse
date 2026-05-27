import Foundation
import Testing

@testable import Longhouse

struct TimelineCacheStoreTests {
    @Test
    func cacheRoundTripsForMatchingServer() throws {
        let defaults = try makeDefaults()
        let savedAt = Date(timeIntervalSince1970: 1_800)
        let sessions = [makeSummary(id: "session-1"), makeSummary(id: "session-2")]

        TimelineCacheStore.save(
            sessions: sessions,
            serverURL: " https://example.longhouse.ai/ ",
            defaults: defaults,
            now: savedAt
        )

        let cached = try #require(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt.addingTimeInterval(60)
        ))
        #expect(cached.savedAt == savedAt)
        #expect(cached.sessions.map(\.id) == ["session-1", "session-2"])
    }

    @Test
    func cacheRejectsDifferentServerOrIdentity() throws {
        let defaults = try makeDefaults()
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            identity: "user-a",
            defaults: defaults,
            now: savedAt
        )

        #expect(TimelineCacheStore.load(
            serverURL: "https://other.longhouse.ai",
            identity: "user-a",
            defaults: defaults,
            now: savedAt
        ) == nil)
        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            identity: "user-b",
            defaults: defaults,
            now: savedAt
        ) == nil)
    }

    @Test
    func cacheRejectsExpiredSnapshots() throws {
        let defaults = try makeDefaults()
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt
        )

        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt.addingTimeInterval(24 * 60 * 60 + 1)
        ) == nil)
    }

    @Test
    func cacheBoundsStoredSessions() throws {
        let defaults = try makeDefaults()
        let sessions = (0..<45).map { makeSummary(id: "session-\($0)") }
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: sessions,
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt
        )

        let cached = try #require(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt
        ))
        #expect(cached.sessions.count == 40)
        #expect(cached.sessions.first?.id == "session-0")
        #expect(cached.sessions.last?.id == "session-39")
    }

    @Test
    func clearRemovesMatchingServerOnly() throws {
        let defaults = try makeDefaults()
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt
        )
        TimelineCacheStore.clear(serverURL: "https://other.longhouse.ai", defaults: defaults)
        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt
        ) != nil)

        TimelineCacheStore.clear(serverURL: "https://example.longhouse.ai", defaults: defaults)
        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            defaults: defaults,
            now: savedAt
        ) == nil)
    }

    private func makeDefaults() throws -> UserDefaults {
        let suiteName = "ai.longhouse.timeline-cache-tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return defaults
    }

    private func makeSummary(id: String) -> SessionSummary {
        SessionSummary(
            id: id,
            title: "Session \(id)",
            presenceState: "idle",
            provider: "codex",
            project: "zerg",
            lastActivityAt: "2026-05-21T10:00:00Z",
            summary: "Cached timeline row",
            summaryStatus: "ready",
            firstUserMessage: "Start work",
            userState: "active",
            status: "idle",
            displayPhase: "Idle",
            presenceTool: nil,
            activeTool: nil,
            gitBranch: "main",
            homeLabel: "On this Mac",
            headOriginLabel: "On this Mac",
            timelineAnchorAt: "2026-05-21T10:00:00Z",
            userMessages: 2,
            toolCalls: 1,
            liveControlAvailable: true,
            hostReattachAvailable: true,
            replyToLiveSessionAvailable: true,
            runtimeDisplay: SessionRuntimeDisplay.widgetPlaceholder(state: "idle", phase: "Idle", tone: "idle"),
            timelineCard: nil
        )
    }
}
