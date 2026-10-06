import Foundation

enum HostLifecycleState: String, Codable, Hashable, Sendable {
    case updating
    case serving
}

enum HostAdmission: String, Codable, Hashable, Sendable {
    case open
    case pending
    case draining
}

struct HostLifecycle: Codable, Equatable, Hashable, Sendable {
    let state: HostLifecycleState
    let runtimeEpoch: String
    let attemptId: String?
    let phase: String?
    let expectedBackBy: String?
    let deadline: String?
    let cutoff: String?
}

struct RuntimeRestartingResponse: Decodable, Sendable {
    let code: String
    let retryable: Bool
    let runtimeEpoch: String?
    let admission: HostAdmission?
    let claim: HostLifecycle?
}

enum HostLinkCopy {
    static let updatingHeadline = "Longhouse is updating"
    static let updatingDetail = "Your agents keep running on this Mac. Nothing is lost; updates resume in a few seconds."
    static let updatingDock = "Updates paused · Longhouse is updating"
    static let slowUpdateHeadline = "Update is taking longer than usual"
    static let sendQueued = "Queued, sends when the update finishes"
}
