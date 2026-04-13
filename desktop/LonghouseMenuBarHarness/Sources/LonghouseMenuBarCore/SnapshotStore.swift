import Foundation
import SwiftUI

@MainActor
public final class SnapshotStore: ObservableObject {
    @Published public private(set) var snapshot: HealthSnapshot?
    @Published public private(set) var loadError: String?
    @Published public private(set) var isLoading: Bool

    private let source: any HealthSnapshotSource
    private var refreshTask: Task<Void, Never>?

    public init(source: any HealthSnapshotSource) {
        self.source = source
        self.isLoading = false
        if source is CLIHealthSnapshotSource {
            refresh()
        } else {
            do {
                snapshot = try source.load()
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
                self.loadError = nil
            case let .failure(message):
                self.loadError = message
            }

            self.isLoading = false
        }
    }

    private static func loadSnapshot(from source: any HealthSnapshotSource) async -> SnapshotLoadResult {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: .success(try source.load()))
                } catch {
                    continuation.resume(returning: .failure(error.localizedDescription))
                }
            }
        }
    }
}

private enum SnapshotLoadResult: Sendable {
    case success(HealthSnapshot)
    case failure(String)
}
