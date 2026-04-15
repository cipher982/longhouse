import Foundation
import SwiftUI

public enum SnapshotRefreshReason: Sendable {
    case initial
    case background
    case manual
}

@MainActor
public final class SnapshotStore: ObservableObject {
    public static let historyRetentionMinutes = 30

    @Published public private(set) var snapshot: HealthSnapshot?
    @Published public private(set) var history: [SnapshotHistorySample]
    @Published public private(set) var loadError: String?
    @Published public private(set) var isInitialLoading: Bool
    @Published public private(set) var isManualRefreshActive: Bool
    @Published public private(set) var presentationDate: Date
    @Published public private(set) var feedback: HealthActionFeedback?

    private let source: any HealthSnapshotSource
    private var refreshTask: Task<Void, Never>?
    private var activeRefreshReason: SnapshotRefreshReason?
    private var queuedManualRefresh = false
    private var presentationTimer: Timer?
    private var presentationConsumerCount = 0
    private static let historyRetentionSeconds: TimeInterval = Double(historyRetentionMinutes * 60)
    private static let maxHistorySamples = 180

    public init(source: any HealthSnapshotSource) {
        self.source = source
        self.history = []
        self.isInitialLoading = false
        self.isManualRefreshActive = false
        self.presentationDate = Date()
        self.feedback = nil
        if source is CLIHealthSnapshotSource {
            refresh(reason: .initial)
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

    public func refresh(reason: SnapshotRefreshReason = .background) {
        if let activeRefreshReason {
            if reason == .manual && activeRefreshReason != .manual {
                queuedManualRefresh = true
                isManualRefreshActive = true
            }
            return
        }

        startRefresh(reason: reason)
    }

    public func beginPresentationUpdates() {
        presentationConsumerCount += 1
        presentationDate = Date()
        guard presentationTimer == nil else {
            return
        }

        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.presentationConsumerCount > 0 else {
                    return
                }
                self.presentationDate = Date()
            }
        }
        timer.tolerance = 0.2
        RunLoop.main.add(timer, forMode: .common)
        presentationTimer = timer
    }

    public func endPresentationUpdates() {
        presentationConsumerCount = max(0, presentationConsumerCount - 1)
        guard presentationConsumerCount == 0 else {
            return
        }

        presentationTimer?.invalidate()
        presentationTimer = nil
    }

    public func setFeedback(_ feedback: HealthActionFeedback?) {
        self.feedback = feedback
    }

    public func clearFeedback() {
        feedback = nil
    }

    private func startRefresh(reason: SnapshotRefreshReason) {
        activeRefreshReason = reason
        if snapshot == nil {
            isInitialLoading = true
        }
        if reason == .manual {
            isManualRefreshActive = true
        }

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

            self.completeRefresh(reason: reason)
        }
    }

    private func completeRefresh(reason: SnapshotRefreshReason) {
        activeRefreshReason = nil
        isInitialLoading = false

        if queuedManualRefresh {
            queuedManualRefresh = false
            startRefresh(reason: .manual)
            return
        }

        if reason == .manual {
            isManualRefreshActive = false
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
