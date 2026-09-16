import CryptoKit
import Foundation
import SwiftUI

/// The complete user intent held locally until the Runtime Host has given the
/// client an authoritative disposition.  This is deliberately separate from
/// the transcript cache: an input is an obligation, not derived display data.
struct PendingInputIntent: Codable, Identifiable, Sendable, Equatable {
    struct Attachment: Codable, Sendable, Equatable, Identifiable {
        let id: UUID
        let filename: String
        let data: Data
        let mimeType: String

        var byteSize: Int { data.count }
    }

    let clientRequestId: String
    let serverURL: String
    let authGeneration: String
    let sessionId: String
    let text: String
    let intent: String
    let attachments: [Attachment]
    let createdAt: Date

    var id: String { clientRequestId }

    init(
        clientRequestId: String,
        serverURL: String,
        authGeneration: String,
        sessionId: String,
        text: String,
        intent: String,
        attachments: [Attachment],
        createdAt: Date
    ) {
        self.clientRequestId = clientRequestId
        self.serverURL = TranscriptSnapshot.normalizedServerURL(serverURL)
        self.authGeneration = authGeneration
        self.sessionId = sessionId
        self.text = text
        self.intent = intent
        self.attachments = attachments
        self.createdAt = createdAt
    }

    init(
        clientRequestId: String,
        serverURL: String,
        authGeneration: String,
        sessionId: String,
        text: String,
        intent: String,
        attachments: [ComposerAttachment],
        createdAt: Date
    ) {
        self.init(
            clientRequestId: clientRequestId,
            serverURL: serverURL,
            authGeneration: authGeneration,
            sessionId: sessionId,
            text: text,
            intent: intent,
            attachments: attachments.map {
                Attachment(id: $0.id, filename: $0.filename, data: $0.data, mimeType: $0.mimeType)
            },
            createdAt: createdAt
        )
    }

    func composerAttachments() -> [ComposerAttachment] {
        attachments.map {
            ComposerAttachment(
                id: $0.id,
                filename: $0.filename,
                data: $0.data,
                mimeType: $0.mimeType,
                thumbnail: UIImage(data: $0.data)
            )
        }
    }
}

/// Atomic, account-scoped storage for sends that may have crossed the network
/// boundary without a response. Files are keyed by server, auth generation,
/// session and client request ID, so changing tenant/login cannot replay an
/// old intent into a new account.
struct PendingInputStore: Sendable {
    static let shared = PendingInputStore()
    static let schemaVersion = 1

    private struct Envelope: Codable, Sendable {
        let schemaVersion: Int
        let intent: PendingInputIntent
    }

    private let directory: URL

    init(directory: URL? = nil) {
        if let directory {
            self.directory = directory
        } else {
            let base = (try? FileManager.default.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )) ?? FileManager.default.temporaryDirectory
            self.directory = base.appendingPathComponent("PendingInputIntents", isDirectory: true)
        }
        ensureDirectory()
    }

    /// Writes the complete payload synchronously and atomically. Callers must
    /// finish this method before initiating the first network request.
    @discardableResult
    func save(_ intent: PendingInputIntent) -> Bool {
        ensureDirectory()
        let envelope = Envelope(schemaVersion: Self.schemaVersion, intent: intent)
        guard let data = try? Self.encoder.encode(envelope) else { return false }
        do {
            try data.write(to: fileURL(for: intent), options: .atomic)
            return true
        } catch {
            return false
        }
    }

    func load(serverURL: String, sessionId: String, authGeneration: String) -> [PendingInputIntent] {
        let normalized = TranscriptSnapshot.normalizedServerURL(serverURL)
        let prefix = Self.scopePrefix(serverURL: normalized, sessionId: sessionId, authGeneration: authGeneration)
        let files = (try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil
        )) ?? []
        return files.compactMap { url -> PendingInputIntent? in
            guard url.pathExtension == "json",
                  let data = try? Data(contentsOf: url),
                  let envelope = try? Self.decoder.decode(Envelope.self, from: data),
                  envelope.schemaVersion == Self.schemaVersion else {
                return nil
            }
            let intent = envelope.intent
            guard Self.scopePrefix(
                serverURL: intent.serverURL,
                sessionId: intent.sessionId,
                authGeneration: intent.authGeneration
            ) == prefix else {
                return nil
            }
            return intent
        }.sorted { lhs, rhs in
            if lhs.createdAt == rhs.createdAt { return lhs.clientRequestId < rhs.clientRequestId }
            return lhs.createdAt < rhs.createdAt
        }
    }

    func remove(_ intent: PendingInputIntent) {
        try? FileManager.default.removeItem(at: fileURL(for: intent))
    }

    func remove(
        serverURL: String,
        sessionId: String,
        authGeneration: String,
        clientRequestId: String
    ) {
        let intent = PendingInputIntent(
            clientRequestId: clientRequestId,
            serverURL: serverURL,
            authGeneration: authGeneration,
            sessionId: sessionId,
            text: "",
            intent: "auto",
            attachments: [],
            createdAt: .distantPast
        )
        remove(intent)
    }

    func waitForPendingWrites() {}

    private func fileURL(for intent: PendingInputIntent) -> URL {
        let key = [
            intent.serverURL,
            intent.authGeneration,
            intent.sessionId,
            intent.clientRequestId,
        ].joined(separator: "|")
        let digest = SHA256.hash(data: Data(key.utf8))
        let name = digest.map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent("\(name).json", isDirectory: false)
    }

    private static func scopePrefix(serverURL: String, sessionId: String, authGeneration: String) -> String {
        [TranscriptSnapshot.normalizedServerURL(serverURL), authGeneration, sessionId].joined(separator: "|")
    }

    private func ensureDirectory() {
        guard !FileManager.default.fileExists(atPath: directory.path) else { return }
        try? FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
    }

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
}
