import Foundation
import Testing
@testable import Longhouse

/// The mobile detail surface reads a real server payload, not a hand-built
/// domain model. The fixture is emitted from the server's own Pydantic models,
/// so it carries `session_state` in the nested contract shape. Decoding the
/// domain `SessionDetail` straight off the wire silently lost those facts and
/// made every managed Helm session render as a read-only import.
struct SessionWireContractTests {
    private func loadFixtureData(_ name: String) throws -> Data {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/session-detail")
            .appendingPathComponent(name)
        return try Data(contentsOf: fixtureURL)
    }

    private func loadManagedHelmTail() throws -> SessionMobileTailResponse {
        try LonghouseAPI.decodeSessionMobileTail(loadFixtureData("managed-helm-mobile-tail.json"))
    }

    @Test
    func managedHelmMobileTailKeepsControlFacts() throws {
        let detail = try loadManagedHelmTail().session

        #expect(detail.stateFacts.mode == "helm")
        #expect(detail.stateFacts.controlOwnership == "owned")
        #expect(detail.stateFacts.controlConnection == "connected")
        #expect(detail.stateFacts.sendInput.isAvailable)
        #expect(detail.stateFacts.activityState == "quiescent")
        #expect(detail.stateFacts.access?.label == "Live control")
    }

    @Test
    func managedHelmMobileTailIsSteerableNotReadOnly() throws {
        let detail = try loadManagedHelmTail().session

        #expect(detail.canSendLive)
        #expect(!detail.isReadOnly)
        #expect(!detail.isControlOffline)
        #expect(detail.controlHealthMessage == nil)
        #expect(detail.runtimeCapabilityLabel == "Live control")
    }

    /// Storage-v2 event ids are strings; the legacy path sends integers. Both
    /// have to survive the wire without failing the whole tail decode.
    @Test
    func mobileTailAcceptsStringEventIdentifiers() throws {
        let tail = try loadManagedHelmTail()

        #expect(tail.snapshotEventId == "01J9ZQ8N2K7F3W")
        #expect(tail.workspaceRevision?.latestEventId == "01J9ZQ8N2K7F3W")
    }
}
