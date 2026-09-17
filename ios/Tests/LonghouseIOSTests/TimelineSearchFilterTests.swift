import Foundation
import Testing

@testable import Longhouse

/// The keystroke path: filtering the rows already on screen. This is the part
/// of search that has to be right without a network, so it is tested without
/// one.
struct TimelineSearchFilterTests {
    @Test
    func emptyQueryKeepsEveryResidentRow() {
        let sessions = [
            makeSession(id: "a", title: "Deploy pipeline"),
            makeSession(id: "b", title: "Schema migration"),
            makeSession(id: "c", title: "Provider chips"),
        ]

        #expect(filterTimelineSessions(sessions, query: "").map(\.id) == ["a", "b", "c"])
        #expect(filterTimelineSessions(sessions, query: "   ").map(\.id) == ["a", "b", "c"])
    }

    @Test
    func matchesTitleRegardlessOfCaseOrDiacritics() {
        let sessions = [makeSession(id: "a", title: "Réparer le déploiement")]

        #expect(filterTimelineSessions(sessions, query: "reparer").map(\.id) == ["a"])
        #expect(filterTimelineSessions(sessions, query: "DEPLOIEMENT").map(\.id) == ["a"])
        #expect(filterTimelineSessions(sessions, query: "deploiement").map(\.id) == ["a"])
    }

    @Test
    func everyTokenMustMatchAndMayMatchADifferentField() {
        let sessions = [
            makeSession(id: "match", title: "deploy pipeline", project: "zerg"),
            makeSession(id: "wrong-project", title: "deploy pipeline", project: "citi"),
            makeSession(id: "missing-token", title: "schema migration", project: "zerg"),
        ]

        // Tokens narrow rather than widen, and their order is not part of the
        // query language.
        #expect(filterTimelineSessions(sessions, query: "zerg deploy").map(\.id) == ["match"])
        #expect(filterTimelineSessions(sessions, query: "deploy zerg").map(\.id) == ["match"])
        #expect(filterTimelineSessions(sessions, query: "zerg").map(\.id) == ["match", "missing-token"])
    }

    @Test
    func matchesTheFieldsTheCardActuallyShows() {
        let machine = makeSession(id: "machine", title: "Unrelated", deviceId: "cube")
        let branch = makeSession(id: "branch", title: "Unrelated", gitBranch: "feat/search-ux")
        let prompt = makeSession(id: "prompt", title: "Unrelated", firstUserMessage: "why is search slow")
        let snippet = makeSession(id: "snippet", title: "Unrelated", matchSnippet: "matched provider channel")
        let sessions = [machine, branch, prompt, snippet]

        #expect(filterTimelineSessions(sessions, query: "cube").map(\.id) == ["machine"])
        #expect(filterTimelineSessions(sessions, query: "search-ux").map(\.id) == ["branch"])
        #expect(filterTimelineSessions(sessions, query: "why is search").map(\.id) == ["prompt"])
        #expect(filterTimelineSessions(sessions, query: "provider channel").map(\.id) == ["snippet"])
    }

    @Test
    func aTokenNobodyMatchesFiltersEverythingOut() {
        let sessions = [
            makeSession(id: "a", title: "Deploy pipeline"),
            makeSession(id: "b", title: "Schema migration"),
        ]

        #expect(filterTimelineSessions(sessions, query: "nomatchhere").isEmpty)
    }
}

private func makeSession(
    id: String,
    title: String,
    project: String? = "zerg",
    provider: String? = "codex",
    firstUserMessage: String? = nil,
    matchSnippet: String? = nil,
    deviceId: String? = nil,
    gitBranch: String? = nil
) -> SessionSummary {
    SessionSummary(
        id: id,
        threadId: "thread-\(id)",
        title: title,
        presenceState: "running",
        provider: provider,
        project: project,
        lastActivityAt: "2026-06-02T14:00:00Z",
        firstUserMessage: firstUserMessage,
        deviceId: deviceId,
        matchSnippet: matchSnippet,
        gitBranch: gitBranch,
        timelineAnchorAt: "2026-06-02T14:00:00Z",
        userMessages: 1,
        toolCalls: 1,
        runtimeDisplay: SessionRuntimeDisplay(
            truthTier: "live",
            signalTier: "live",
            state: "running",
            tone: "thinking",
            headline: "Thinking",
            detail: nil,
            phaseLabel: "Thinking",
            compactToolLabel: nil,
            isLive: true,
            isExecuting: true,
            needsAttention: false,
            isIdle: false,
            isStalled: false,
            isManagedLocalTruth: true,
            hasSignal: true,
            controlPath: "managed",
            activityRecency: "live",
            lifecycle: "open",
            hostState: "online",
            terminalReason: nil
        )
    )
}
