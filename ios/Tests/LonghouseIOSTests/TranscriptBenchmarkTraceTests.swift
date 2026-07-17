import Foundation
import Testing
@testable import Longhouse

@Suite("Transcript benchmark trace")
struct TranscriptBenchmarkTraceTests {
    @Test
    func coreTraceIsDeterministicAndAppendOnly() {
        let initial = TranscriptBenchmarkTrace.initialEvents()
        let snapshots = TranscriptBenchmarkTrace.streamingSnapshots()

        #expect(initial.count == 120)
        #expect(Set(initial.map(\.id)).count == 120)
        #expect(snapshots.count == 120)
        #expect(snapshots.last?.utf8.count == 12_000)
        #expect(zip(snapshots, snapshots.dropFirst()).allSatisfy { previous, next in
            next.hasPrefix(previous)
        })
    }

    @Test
    func prependRowsUseASeparateIdentityNamespace() {
        let initialIDs = Set(TranscriptBenchmarkTrace.initialEvents().map(\.id))
        let older = TranscriptBenchmarkTrace.olderEvents()

        #expect(older.count == 50)
        #expect(Set(older.map(\.id)).count == 50)
        #expect(older.allSatisfy { $0.id.hasPrefix("benchmark-older-") })
        #expect(initialIDs.isDisjoint(with: older.map(\.id)))
    }

    @Test
    func candidateLabelsCannotMasqueradeAsImplementedRenderers() {
        #expect(TranscriptBenchmarkRendererKind.snapshotWebKit.isImplemented)
        #expect(!TranscriptBenchmarkRendererKind.retainedWebKit.isImplemented)
        #expect(!TranscriptBenchmarkRendererKind.nativeUIKit.isImplemented)
        #expect(TranscriptBenchmarkRendererKind.snapshotWebKit.semanticTier == "production")
        #expect(TranscriptBenchmarkRendererKind.nativeUIKit.semanticTier == "mechanical-lower-bound")
    }
}
