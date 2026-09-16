import Foundation

struct BugReportDraft: Codable, Sendable {
    let serverURL: String
    let sourceSessionID: String
    var clientReportID: String?
    var description: String
    var screenshotData: Data?
    var additionalImages: [Data]
}

struct BugReportHandoff: Codable, Sendable {
    let serverURL: String
    let sourceSessionID: String?
    let reportID: String
    let sessionID: String
    let deviceID: String
    let provider: String
    let cwd: String
    let clientRequestID: String
}

enum BugReportLocalStore {
    private static let directoryName = "BugReports"
    private static let draftName = "draft.json"
    private static let handoffName = "handoff.json"

    static func saveDraft(_ draft: BugReportDraft) {
        save(draft, name: draftName)
    }

    static func loadDraft() -> BugReportDraft? {
        load(BugReportDraft.self, name: draftName)
    }

    static func clearDraft() {
        remove(name: draftName)
    }

    static func saveHandoff(_ handoff: BugReportHandoff) {
        save(handoff, name: handoffName)
    }

    static func loadHandoff() -> BugReportHandoff? {
        load(BugReportHandoff.self, name: handoffName)
    }

    static func clearHandoff() {
        remove(name: handoffName)
    }

    private static func save<T: Encodable>(_ value: T, name: String) {
        do {
            let directory = try storageDirectory()
            let data = try JSONEncoder().encode(value)
            try data.write(to: directory.appendingPathComponent(name), options: [.atomic, .completeFileProtection])
        } catch {
            // A local draft is best effort; the report screen keeps the live copy.
        }
    }

    private static func load<T: Decodable>(_ type: T.Type, name: String) -> T? {
        guard let directory = try? storageDirectory(),
              let data = try? Data(contentsOf: directory.appendingPathComponent(name))
        else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    private static func remove(name: String) {
        guard let directory = try? storageDirectory() else { return }
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(name))
    }

    private static func storageDirectory() throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base.appendingPathComponent(directoryName, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }
}
