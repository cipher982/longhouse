import Foundation

/// Splits a byte stream into SSE lines the way the spec reads them: a line
/// ends at LF, CRLF or CR, and an empty line is an event boundary that must
/// be delivered as an empty string.
///
/// `URLSession.AsyncBytes.lines` cannot be used for this: it collapses
/// consecutive terminators, so the blank line between two CRLF-delimited
/// events never surfaces and a parser waiting on it never dispatches.
/// The server frames every event with CRLF.
enum SSELineReader {
    static func lines<Bytes: AsyncSequence & Sendable>(
        from bytes: Bytes
    ) -> AsyncThrowingStream<String, Error> where Bytes.Element == UInt8 {
        AsyncThrowingStream { continuation in
            let task = Task {
                var pending: [UInt8] = []
                var sawCarriageReturn = false
                do {
                    for try await byte in bytes {
                        if Task.isCancelled { break }
                        switch byte {
                        case 0x0A: // LF, or the second half of CRLF
                            if !sawCarriageReturn {
                                continuation.yield(Self.decode(pending))
                                pending.removeAll(keepingCapacity: true)
                            }
                            sawCarriageReturn = false
                        case 0x0D: // CR ends the line on its own; a following LF is absorbed
                            continuation.yield(Self.decode(pending))
                            pending.removeAll(keepingCapacity: true)
                            sawCarriageReturn = true
                        default:
                            sawCarriageReturn = false
                            pending.append(byte)
                        }
                    }
                    if !pending.isEmpty {
                        continuation.yield(Self.decode(pending))
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func decode(_ bytes: [UInt8]) -> String {
        String(decoding: bytes, as: UTF8.self)
    }
}
