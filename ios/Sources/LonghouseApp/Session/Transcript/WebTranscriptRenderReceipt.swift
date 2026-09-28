import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

struct WebTranscriptPreparedPayload: Sendable {
    let base64: String
    let payloadByteSize: Int
    let rowCount: Int
    let latestItemId: String?
    let payloadFingerprint: String
    let prepareDurationMs: Int
    let contentRevision: UInt64
    let transcriptReadThrough: String?
    let retryRevision: UInt64
    let sourceRevision: Int?
    let sourceOperation: String?

    init(
        base64: String,
        payloadByteSize: Int,
        rowCount: Int,
        latestItemId: String?,
        payloadFingerprint: String,
        prepareDurationMs: Int,
        contentRevision: UInt64,
        transcriptReadThrough: String?,
        retryRevision: UInt64 = 0,
        sourceRevision: Int?,
        sourceOperation: String?
    ) {
        self.base64 = base64
        self.payloadByteSize = payloadByteSize
        self.rowCount = rowCount
        self.latestItemId = latestItemId
        self.payloadFingerprint = payloadFingerprint
        self.prepareDurationMs = prepareDurationMs
        self.contentRevision = contentRevision
        self.transcriptReadThrough = transcriptReadThrough
        self.retryRevision = retryRevision
        self.sourceRevision = sourceRevision
        self.sourceOperation = sourceOperation
    }
}

struct WebTranscriptRenderReceipt: Equatable, Sendable {
    let contentRevision: UInt64
    let transcriptReadThrough: String?
    let retryRevision: UInt64
    let payloadFingerprint: String
    let latestItemId: String?

    init(
        contentRevision: UInt64,
        transcriptReadThrough: String?,
        retryRevision: UInt64 = 0,
        payloadFingerprint: String,
        latestItemId: String?
    ) {
        self.contentRevision = contentRevision
        self.transcriptReadThrough = transcriptReadThrough
        self.retryRevision = retryRevision
        self.payloadFingerprint = payloadFingerprint
        self.latestItemId = latestItemId
    }
}
