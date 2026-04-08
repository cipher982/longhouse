import Foundation
import SwiftUI

@MainActor
public final class SnapshotStore: ObservableObject {
    @Published public private(set) var snapshot: HealthSnapshot?
    @Published public private(set) var loadError: String?

    private let source: any HealthSnapshotSource

    public init(source: any HealthSnapshotSource) {
        self.source = source
        refresh()
    }

    public func refresh() {
        do {
            snapshot = try source.load()
            loadError = nil
        } catch {
            snapshot = nil
            loadError = error.localizedDescription
        }
    }
}
