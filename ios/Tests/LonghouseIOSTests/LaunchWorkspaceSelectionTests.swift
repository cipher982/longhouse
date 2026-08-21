import Foundation
import Testing
@testable import Longhouse

struct LaunchWorkspaceSelectionTests {
    private func workspace(_ path: String, score: Double) -> WorkspaceSuggestion {
        WorkspaceSuggestion(path: path, label: URL(fileURLWithPath: path).lastPathComponent, score: score)
    }

    @Test
    func freshRankingAlwaysReplacesImplicitCachedDefault() {
        let fresh = [
            workspace("/Users/example/git/zerg", score: 200),
            workspace("/Users/example/git/old", score: 100),
        ]

        let selection = resolveFreshWorkspaceSelection(
            currentPath: "/Users/example/git/old",
            source: .implicitDefault,
            suggestions: fresh
        )

        #expect(selection == WorkspaceSelectionResolution(
            path: "/Users/example/git/zerg",
            source: .implicitDefault
        ))
    }

    @Test
    func freshRankingReplacesRemovedImplicitDefault() {
        let selection = resolveFreshWorkspaceSelection(
            currentPath: "/Users/example/.longhouse/canaries/provider-live/cursor/workspace",
            source: .implicitDefault,
            suggestions: [workspace("/Users/example/git/zerg", score: 200)]
        )

        #expect(selection.path == "/Users/example/git/zerg")
        #expect(selection.source == .implicitDefault)
    }

    @Test
    func freshRankingPreservesExplicitValidChoice() {
        let fresh = [
            workspace("/Users/example/git/zerg", score: 200),
            workspace("/Users/example/git/g55", score: 100),
        ]

        let selection = resolveFreshWorkspaceSelection(
            currentPath: "/Users/example/git/g55",
            source: .explicitUserChoice,
            suggestions: fresh
        )

        #expect(selection == WorkspaceSelectionResolution(
            path: "/Users/example/git/g55",
            source: .explicitUserChoice
        ))
    }

    @Test
    func freshRankingPreservesExplicitAbsoluteManualChoice() {
        let selection = resolveFreshWorkspaceSelection(
            currentPath: "/Volumes/dev/project",
            source: .explicitUserChoice,
            suggestions: [workspace("/Users/example/git/zerg", score: 200)]
        )

        #expect(selection.path == "/Volumes/dev/project")
        #expect(selection.source == .explicitUserChoice)
    }

    @Test
    func versionTwoCacheIgnoresVersionOneKeyAndRoundTrips() throws {
        let suite = "LaunchWorkspaceSelectionTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set(Data("stale".utf8), forKey: "longhouse.launch.workspaces.cache.v1")

        #expect(WorkspaceSuggestionsCacheStore.load(
            serverURL: "https://demo.longhouse.ai",
            deviceId: "cinder",
            defaults: defaults
        ) == nil)

        let expected = [workspace("/Users/example/git/zerg", score: 200)]
        WorkspaceSuggestionsCacheStore.save(
            workspaces: expected,
            serverURL: "https://demo.longhouse.ai",
            deviceId: "cinder",
            defaults: defaults
        )
        #expect(WorkspaceSuggestionsCacheStore.load(
            serverURL: "https://demo.longhouse.ai",
            deviceId: "cinder",
            defaults: defaults
        ) == expected)
    }
}
