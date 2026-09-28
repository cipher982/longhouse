import Foundation
import OSLog

/// One batch of app lifecycle marks bound for `/api/telemetry/client-diagnostics`.
struct ClientDiagnosticsPayload: Encodable, Sendable {
    struct Entry: Encodable, Sendable {
        let at_ms: Int64
        let stage: String
        let detail: String?
        let session_id: String?
    }

    let surface: String
    let device_label: String?
    let app_build: String?
    let entries: [Entry]
}

struct BugReportUploadFile: Sendable {
    let filename: String
    let mimeType: String
    let data: Data
}

struct BugReportUploadedFile: Decodable, Sendable {
    let name: String
    let mimeType: String
    let byteSize: Int
    let sha256: String
    let kind: String
}

struct BugReportUploadResponse: Decodable, Sendable {
    let reportId: String
    let createdAt: String
    let sourceSessionId: String?
    let files: [BugReportUploadedFile]
}
