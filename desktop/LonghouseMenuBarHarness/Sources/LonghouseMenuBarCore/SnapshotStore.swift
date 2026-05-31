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
    public static let bootGraceSeconds: TimeInterval = 10
    public static let transientEngineStatusAttentionGraceSeconds: TimeInterval = 60
    public static let staleCacheFailureSeconds: TimeInterval = 120

    @Published public private(set) var snapshot: HealthSnapshot?
    @Published public private(set) var history: [SnapshotHistorySample]
    @Published public private(set) var loadError: String?
    @Published public private(set) var isInitialLoading: Bool
    @Published public private(set) var isManualRefreshActive: Bool
    @Published public private(set) var isBooting: Bool
    @Published public private(set) var isTransientEngineStatusSettling: Bool
    @Published public private(set) var presentationDate: Date
    @Published public private(set) var feedback: HealthActionFeedback?

    private let source: any HealthSnapshotSource
    private var refreshTask: Task<Void, Never>?
    private var bootGraceTask: Task<Void, Never>?
    private var transientEngineStatusGraceTask: Task<Void, Never>?
    private var transientEngineStatusFirstSeenAt: Date?
    private var activeRefreshReason: SnapshotRefreshReason?
    private var queuedManualRefresh = false
    private var presentationTimer: Timer?
    private var presentationConsumerCount = 0
    private let cacheURL: URL?
    private static let historyRetentionSeconds: TimeInterval = Double(historyRetentionMinutes * 60)
    private static let maxHistorySamples = 180

    public init(source: any HealthSnapshotSource, cacheURL: URL? = nil) {
        self.source = source
        self.cacheURL = cacheURL ?? Self.defaultCacheURL(for: source)
        self.history = []
        self.isInitialLoading = false
        self.isManualRefreshActive = false
        self.isBooting = false
        self.isTransientEngineStatusSettling = false
        self.presentationDate = Date()
        self.feedback = nil
        if let cachedSnapshot = Self.loadCachedSnapshot(from: self.cacheURL) {
            snapshot = cachedSnapshot
            appendHistorySample(for: cachedSnapshot)
            loadError = nil
        }
        if source is CLIHealthSnapshotSource {
            isBooting = true
            scheduleBootGraceTimeout()
            refresh(reason: .initial)
        } else {
            do {
                let loadedSnapshot = try source.load()
                snapshot = loadedSnapshot
                appendHistorySample(for: loadedSnapshot)
                persistCachedSnapshot(loadedSnapshot)
                loadError = nil
                updateTransientEngineStatusSettling(for: loadedSnapshot)
            } catch {
                loadError = error.localizedDescription
            }
        }
    }

    deinit {
        refreshTask?.cancel()
        bootGraceTask?.cancel()
        transientEngineStatusGraceTask?.cancel()
    }

    private func scheduleBootGraceTimeout() {
        bootGraceTask?.cancel()
        bootGraceTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.bootGraceSeconds))
            guard !Task.isCancelled, let self else {
                return
            }
            self.isBooting = false
        }
    }

    private func exitBootingIfReady(for snapshot: HealthSnapshot) {
        guard isBooting else {
            return
        }
        if snapshot.parsedSeverity == .green {
            isBooting = false
            bootGraceTask?.cancel()
            bootGraceTask = nil
        }
    }

    private func updateTransientEngineStatusSettling(for snapshot: HealthSnapshot) {
        guard snapshot.isTransientEngineStatusOnlyAttention else {
            transientEngineStatusFirstSeenAt = nil
            transientEngineStatusGraceTask?.cancel()
            transientEngineStatusGraceTask = nil
            isTransientEngineStatusSettling = false
            return
        }

        let firstSeenAt = transientEngineStatusFirstSeenAt ?? Date()
        transientEngineStatusFirstSeenAt = firstSeenAt
        let elapsed = Date().timeIntervalSince(firstSeenAt)
        let shouldSettle = elapsed < Self.transientEngineStatusAttentionGraceSeconds
        isTransientEngineStatusSettling = shouldSettle
        guard shouldSettle, transientEngineStatusGraceTask == nil else {
            return
        }

        let remaining = max(0, Self.transientEngineStatusAttentionGraceSeconds - elapsed)
        transientEngineStatusGraceTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(remaining))
            guard !Task.isCancelled, let self else {
                return
            }
            if self.snapshot?.isTransientEngineStatusOnlyAttention == true {
                self.isTransientEngineStatusSettling = false
            }
            self.transientEngineStatusGraceTask = nil
        }
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

    public func staleCachedSnapshotFailureMessage(relativeTo referenceDate: Date) -> String? {
        guard let snapshot else {
            return nil
        }
        guard let collectedAt = snapshot.collectedAtDate else {
            if let loadError {
                return "Longhouse status is stale. Refresh failed: \(loadError)"
            }
            return "Longhouse status is stale. The latest snapshot is missing a timestamp."
        }
        let snapshotAgeSeconds = referenceDate.timeIntervalSince(collectedAt)
        guard snapshotAgeSeconds > Self.staleCacheFailureSeconds else {
            return nil
        }
        let age = snapshot.snapshotAgeCompactLabel(relativeTo: referenceDate)
        if let loadError {
            return "Longhouse status is stale. Last successful update was \(age) ago. Refresh failed: \(loadError)"
        }
        return "Longhouse status is stale. Last successful update was \(age) ago. Refresh has not produced a fresh snapshot."
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
                self.persistCachedSnapshot(snapshot)
                self.loadError = nil
                self.updateTransientEngineStatusSettling(for: snapshot)
                self.exitBootingIfReady(for: snapshot)
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

    private static func defaultCacheURL(for source: any HealthSnapshotSource) -> URL? {
        guard source is CLIHealthSnapshotSource else {
            return nil
        }
        guard let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        return appSupport
            .appendingPathComponent("Longhouse", isDirectory: true)
            .appendingPathComponent("MenuBar", isDirectory: true)
            .appendingPathComponent("last-good-snapshot.json")
    }

    private static func loadCachedSnapshot(from cacheURL: URL?) -> HealthSnapshot? {
        guard let cacheURL else {
            return nil
        }
        guard let data = try? Data(contentsOf: cacheURL) else {
            return nil
        }
        return try? HealthSnapshotDecoder.decode(data: data)
    }

    private func persistCachedSnapshot(_ snapshot: HealthSnapshot) {
        guard let cacheURL else {
            return
        }
        do {
            try FileManager.default.createDirectory(
                at: cacheURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            let data = try encoder.encode(snapshot)
            try data.write(to: cacheURL, options: [.atomic])
        } catch {
            // Cache failures should never make the menu bar look unhealthy.
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
