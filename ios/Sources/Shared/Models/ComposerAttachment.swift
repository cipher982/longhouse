import Foundation
import SwiftUI

struct ComposerAttachment: Identifiable, Sendable {
    let id: UUID
    let filename: String
    let data: Data
    let mimeType: String
    let thumbnail: UIImage?

    var byteSize: Int { data.count }
}

enum ComposerAttachmentLimits {
    static let maxAttachments = 4
    static let maxBytes = 2 * 1024 * 1024
}

@MainActor
final class ComposerAttachmentStore: ObservableObject {
    @Published private(set) var attachments: [ComposerAttachment] = []
    @Published private(set) var isProcessing = false
    @Published var errorMessage: String? = nil

    var isEmpty: Bool { attachments.isEmpty }
    var slotsLeft: Int { ComposerAttachmentLimits.maxAttachments - attachments.count }

    /// Compress + add new images, capping at the slot limit. Each input may
    /// already be compressed (PhotosPicker hands us decoded image data) but
    /// we re-encode through ImageCompression for size + dimension control.
    func ingest(rawImages: [(filename: String, data: Data)]) async {
        guard !isProcessing else {
            errorMessage = "Still processing previous selection — try again in a moment."
            return
        }
        guard slotsLeft > 0 else {
            errorMessage = "Max \(ComposerAttachmentLimits.maxAttachments) attachments."
            return
        }
        isProcessing = true
        defer { isProcessing = false }
        for raw in rawImages {
            guard attachments.count < ComposerAttachmentLimits.maxAttachments else { break }
            do {
                let compressed = try await Task.detached(priority: .userInitiated) {
                    try ImageCompression.compress(raw.data)
                }.value
                guard attachments.count < ComposerAttachmentLimits.maxAttachments else { break }
                if compressed.data.count > ComposerAttachmentLimits.maxBytes {
                    let kb = compressed.data.count / 1024
                    errorMessage = "\(raw.filename) is still \(kb) KB after compression (max 2 MB)."
                    continue
                }
                let thumb = UIImage(data: compressed.data)
                attachments.append(
                    ComposerAttachment(
                        id: UUID(),
                        filename: raw.filename,
                        data: compressed.data,
                        mimeType: compressed.mimeType,
                        thumbnail: thumb,
                    )
                )
            } catch {
                errorMessage = "Could not process \(raw.filename): \(error.localizedDescription)"
            }
        }
    }

    func remove(_ id: UUID) {
        attachments.removeAll { $0.id == id }
    }

    func clear() {
        attachments.removeAll()
        errorMessage = nil
    }

    /// Restores the already-compressed bytes from a pending operation without
    /// decoding/re-encoding them. This is used only when local persistence
    /// failed or the user explicitly edits a retained outbox row.
    func restore(_ restored: [ComposerAttachment]) {
        guard !isProcessing else { return }
        attachments = Array(restored.prefix(ComposerAttachmentLimits.maxAttachments))
        errorMessage = nil
    }

    func restore(_ restored: [PendingInputIntent.Attachment]) {
        restore(restored.map {
            ComposerAttachment(
                id: $0.id,
                filename: $0.filename,
                data: $0.data,
                mimeType: $0.mimeType,
                thumbnail: UIImage(data: $0.data)
            )
        })
    }

    func snapshot() -> [ComposerAttachment] { attachments }
}
