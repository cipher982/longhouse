import Foundation
import OSLog

struct SessionInputOperationError: Error, LocalizedError, Sendable, Equatable {
    let statusCode: Int
    let message: String
    let errorCode: String?
    let disposition: SessionInputDisposition
    let deliveryStatus: String?
    let clientRequestId: String?
    let inputId: Int?
    let liveInputId: String?
    let turn: ConsoleTurnReceipt?

    var errorDescription: String? { message }
}

enum LonghouseAPIError: Error {
    case requestFailed
    case httpRejected(status: Int, message: String)
    case notAuthenticated
    case conflict
    case serviceUnavailable
    case upstreamFailed
    case unexpectedResponse(String)
    /// Server returned a structured error payload (e.g. `{"detail": {"error_code": "turn_ended"}}`).
    /// Carries status + code + message so the caller can branch on the
    /// semantic outcome instead of parsing ad-hoc strings.
    case structured(status: Int, errorCode: String, message: String)

    static func from(statusCode: Int) -> LonghouseAPIError {
        switch statusCode {
        case 401:
            return .notAuthenticated
        case 409:
            return .conflict
        case 408, 429:
            return .serviceUnavailable
        case 500...599:
            return .upstreamFailed
        default:
            return .requestFailed
        }
    }

    /// Whether replaying the same handoff request can plausibly change the outcome.
    ///
    /// Structured Console failures are terminal unless the server explicitly
    /// reports an unavailable catalog or an ambiguous dispatch. The request ID
    /// remains stable in both cases, so a retry can only replay the same intent.
    var isRetryableReportHandoff: Bool {
        switch self {
        case .serviceUnavailable, .upstreamFailed, .unexpectedResponse:
            return true
        case .structured(_, let code, _):
            return code == "catalog_unavailable" || code == "turn_start_outcome_unknown"
        case .httpRejected(_, _), .requestFailed, .notAuthenticated, .conflict:
            return false
        }
    }
    var structuredCode: String? {
        guard case .structured(_, let code, _) = self else { return nil }
        return code
    }

    var isRuntimeDraining: Bool {
        structuredCode?.lowercased() == "runtime_draining"
    }

    var isProviderDeliveryUnknown: Bool {
        switch structuredCode?.lowercased() {
        case "delivery_unknown", "input_receipt_unknown":
            return true
        default:
            return false
        }
    }
}

extension LonghouseAPIError: LocalizedError {
    var errorDescription: String? {
        switch self {
        case .requestFailed:
            return "Request failed."
        case .notAuthenticated:
            return "Session expired."
        case .conflict:
            return "Session is busy. Try again in a moment."
        case .serviceUnavailable:
            return "Service is not configured yet."
        case .upstreamFailed:
            return "Generation failed. Try again."
        case .unexpectedResponse(let message):
            return message
        case .httpRejected(_, let message):
            return message.isEmpty ? "Request was rejected." : message
        case .structured(_, _, let message):
            return message.isEmpty ? "Request was rejected." : message
        }
    }
}
