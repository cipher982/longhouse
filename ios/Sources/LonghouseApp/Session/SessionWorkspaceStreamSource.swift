import Foundation

struct SessionWorkspaceStreamSource: Sendable {
    let start: @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>
    let stop: @Sendable () async -> Void
    let clockSkewMs: @Sendable () async -> Int64
    /// Seeds the epoch paired with Last-Event-ID. Existing protocol doubles
    /// may omit this seam and receive a no-op.
    let setStreamEpoch: @Sendable (String?) async -> Void

    init(
        start: @escaping @Sendable () async -> AsyncStream<SessionWorkspaceStream.Event>,
        stop: @escaping @Sendable () async -> Void,
        clockSkewMs: @escaping @Sendable () async -> Int64,
        setStreamEpoch: @escaping @Sendable (String?) async -> Void = { _ in }
    ) {
        self.start = start
        self.stop = stop
        self.clockSkewMs = clockSkewMs
        self.setStreamEpoch = setStreamEpoch
    }

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
            clockSkewMs: { await stream.clockSkewMs() },
            setStreamEpoch: { epoch in await stream.setStreamEpoch(epoch) }
        )
    }
}
