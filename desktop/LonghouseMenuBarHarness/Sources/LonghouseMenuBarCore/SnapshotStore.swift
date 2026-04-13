import Foundation
import SwiftUI

@MainActor
public final class SnapshotStore: ObservableObject {
    public static let historyRetentionMinutes = 30

    @Published public private(set) var snapshot: HealthSnapshot?
    @Published public private(set) var history: [SnapshotHistorySample]
    @Published public private(set) var loadError: String?
    @Published public private(set) var isLoading: Bool

    private let source: any HealthSnapshotSource
    private var refreshTask: Task<Void, Never>?
    private static let historyRetentionSeconds: TimeInterval = Double(historyRetentionMinutes * 60)
    private static let maxHistorySamples = 180

    public init(source: any HealthSnapshotSource) {
        self.source = source
        self.history = []
        self.isLoading = false
        if source is CLIHealthSnapshotSource {
            refresh()
        } else {
            do {
                let loadedSnapshot = try source.load()
                snapshot = loadedSnapshot
                appendHistorySample(for: loadedSnapshot)
                loadError = nil
            } catch {
                loadError = error.localizedDescription
            }
        }
    }

    deinit {
        refreshTask?.cancel()
    }

    public func refresh() {
        guard !isLoading else {
            return
        }

        isLoading = true
        let source = self.source
        refreshTask = Task { [weak self] in
            let result = await Self.loadSnapshot(from: source)
            guard !Task.isCancelled, let self else {
                return
            }

            switch result {
            case let .success(snapshot):
                self.snapshot = snapshot
                self.appendHistorySample(for: snapshot)
                self.loadError = nil
            case let .failure(message):
                self.loadError = message
            }

            self.isLoading = false
        }
    }

    private static func loadSnapshot(from source: any HealthSnapshotSource) async -> SnapshotLoadResult {
        await Task.detached(priority: .userInitiated) {
            do {
                return .success(try source.load())
            } catch {
                return .failure(error.localizedDescription)
            }
        }.value
    }

    private func appendHistorySample(for snapshot: HealthSnapshot) {
        let capturedAt = snapshot.collectedAtDate ?? Date()
        let sample = SnapshotHistorySample(
            capturedAt: capturedAt,
            sessionsRecent: snapshot.activitySummary?.sessionsRecent ?? 0,
            spoolPendingCount: snapshot.engineStatus?.payload?.spoolPendingCount ?? 0,
            outboxCount: snapshot.outboxCount,
            severity: snapshot.parsedSeverity
        )

        if let last = history.last,
           abs(last.capturedAt.timeIntervalSince(sample.capturedAt)) < 0.5,
           last.sessionsRecent == sample.sessionsRecent,
           last.spoolPendingCount == sample.spoolPendingCount,
           last.outboxCount == sample.outboxCount,
           last.severity == sample.severity {
            return
        }

        history.append(sample)
        let cutoff = capturedAt.addingTimeInterval(-Self.historyRetentionSeconds)
        history.removeAll { $0.capturedAt < cutoff }
        if history.count > Self.maxHistorySamples {
            history.removeFirst(history.count - Self.maxHistorySamples)
        }
    }
}

private enum SnapshotLoadResult: Sendable {
    case success(HealthSnapshot)
    case failure(String)
}

public struct SnapshotHistorySample: Equatable, Sendable {
    public let capturedAt: Date
    public let sessionsRecent: Int
    public let spoolPendingCount: Int
    public let outboxCount: Int
    public let severity: HarnessSeverity
}
