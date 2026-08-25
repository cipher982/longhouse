import Foundation
import Testing

@testable import Longhouse

/// Serialized because the legacy-purge tests share one fixed key in
/// `UserDefaults.standard`, which `save` and `clear` purge unconditionally.
/// Run in parallel, one test's `set` lands between the other's purge and its
/// assertion.
@Suite(.serialized)
struct TimelineCacheStoreTests {
    @Test
    func cacheRoundTripsForMatchingServer() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let savedAt = Date(timeIntervalSince1970: 1_800)
        let sessions = [makeSummary(id: "session-1"), makeSummary(id: "session-2")]

        TimelineCacheStore.save(
            sessions: sessions,
            serverURL: " https://example.longhouse.ai/ ",
            directory: directory,
            now: savedAt
        )

        let cached = try #require(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt.addingTimeInterval(60)
        ))
        #expect(cached.savedAt == savedAt)
        #expect(cached.sessions.map(\.id) == ["session-1", "session-2"])
    }

    @Test
    func cacheRejectsDifferentServerOrIdentity() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            identity: "user-a",
            directory: directory,
            now: savedAt
        )

        #expect(TimelineCacheStore.load(
            serverURL: "https://other.longhouse.ai",
            identity: "user-a",
            directory: directory,
            now: savedAt
        ) == nil)
        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            identity: "user-b",
            directory: directory,
            now: savedAt
        ) == nil)
    }

    @Test
    func cacheRejectsExpiredSnapshots() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt
        )

        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt.addingTimeInterval(24 * 60 * 60 + 1)
        ) == nil)
    }

    @Test
    func cacheBoundsStoredSessions() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let sessions = (0..<45).map { makeSummary(id: "session-\($0)") }
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: sessions,
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt
        )

        let cached = try #require(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt
        ))
        #expect(cached.sessions.count == 40)
        #expect(cached.sessions.first?.id == "session-0")
        #expect(cached.sessions.last?.id == "session-39")
    }

    @Test
    func clearRemovesMatchingServerOnly() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let savedAt = Date(timeIntervalSince1970: 1_800)

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt
        )
        TimelineCacheStore.clear(serverURL: "https://other.longhouse.ai", directory: directory)
        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt
        ) != nil)

        TimelineCacheStore.clear(serverURL: "https://example.longhouse.ai", directory: directory)
        #expect(TimelineCacheStore.load(
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: savedAt
        ) == nil)
    }

    /// The security property this store exists for: cached rows carry
    /// transcript-derived text (`summary`, `firstUserMessage`, `matchSnippet`),
    /// so the file must be protected until first unlock and must not ride the
    /// device backup onto another device.
    @Test
    func cacheFileIsProtectedAndExcludedFromBackup() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: Date(timeIntervalSince1970: 1_800)
        )

        let file = directory.appendingPathComponent("sessions.json", isDirectory: false)
        #expect(FileManager.default.fileExists(atPath: file.path))

        // Protection has to be requested by the write itself: an atomic write
        // replaces the file, so nothing carries over from the attributes the
        // directory was created with.
        #expect(TimelineCacheStore.fileWriteOptions.contains(.atomic))
        #expect(TimelineCacheStore.fileWriteOptions
            .contains(.completeFileProtectionUntilFirstUserAuthentication))

        // The simulator does not implement data protection: it reports no
        // protection class for any file, however the file was written. On a
        // device this is the assertion that actually bites; in the simulator
        // gate the constant above is what pins the behavior.
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        if let protection = attributes[.protectionKey] as? FileProtectionType {
            #expect(protection == .completeUntilFirstUserAuthentication)
        }

        // Backup exclusion is real everywhere, including the simulator.
        #expect(isExcludedFromBackup(file))
        #expect(isExcludedFromBackup(directory))
    }

    /// Nothing transcript-derived may sit in `UserDefaults`, which rides the
    /// encrypted backup and restores onto a different device. Saving deletes
    /// the blob left behind by builds that predate the move to disk, and never
    /// writes a new one.
    @Test
    func saveClearsTheLegacyUserDefaultsCache() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let defaults = UserDefaults.standard
        defaults.set(Data("cached transcript text".utf8), forKey: TimelineCacheStore.legacyDefaultsKey)
        defer { defaults.removeObject(forKey: TimelineCacheStore.legacyDefaultsKey) }

        TimelineCacheStore.save(
            sessions: [makeSummary(id: "session-1")],
            serverURL: "https://example.longhouse.ai",
            directory: directory,
            now: Date(timeIntervalSince1970: 1_800)
        )

        #expect(defaults.object(forKey: TimelineCacheStore.legacyDefaultsKey) == nil)
    }

    @Test
    func clearClearsTheLegacyUserDefaultsCache() throws {
        let directory = tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let defaults = UserDefaults.standard
        defaults.set(Data("cached transcript text".utf8), forKey: TimelineCacheStore.legacyDefaultsKey)
        defer { defaults.removeObject(forKey: TimelineCacheStore.legacyDefaultsKey) }

        TimelineCacheStore.clear(directory: directory)

        #expect(defaults.object(forKey: TimelineCacheStore.legacyDefaultsKey) == nil)
    }

    private func tempDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("lh-timeline-cache-tests-\(UUID().uuidString)", isDirectory: true)
    }

    private func isExcludedFromBackup(_ url: URL) -> Bool {
        let values = try? url.resourceValues(forKeys: [.isExcludedFromBackupKey])
        return values?.isExcludedFromBackup ?? false
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
