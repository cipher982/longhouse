import Foundation

struct SessionWorkspaceStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>
    let stop: @Sendable () async -> Void
    let clockSkewMs: @Sendable () async -> Int64

    static func live(
        baseURL: URL,
        sessionId: String,
        sinceSeq: Int? = nil,
        knownWorkspaceFingerprint: String? = nil
    ) -> SessionWorkspaceStreamSource {
        let stream = SessionWorkspaceStream(
            baseURL: baseURL,
            sessionId: sessionId,
            sinceSeq: sinceSeq,
            knownWorkspaceFingerprint: knownWorkspaceFingerprint
        )
        return SessionWorkspaceStreamSource(
            start: { await stream.start() },
            stop: { await stream.stop() },
            clockSkewMs: { await stream.clockSkewMs() }
        )
    }
}
