import Foundation
import Testing
@testable import Longhouse

/// The server frames SSE events with CRLF. `AsyncBytes.lines` collapses the
/// blank line between two events, so the stream parser never saw an event
/// boundary and the phone never dispatched a frame. These pin the reader
/// that replaced it: every terminator style yields the same lines, and the
/// empty line survives.
struct SSELineReaderTests {
    private func lines(_ text: String) async throws -> [String] {
        let bytes = AsyncThrowingStream<UInt8, Error> { continuation in
            for byte in Array(text.utf8) {
                continuation.yield(byte)
            }
            continuation.finish()
        }
        var result: [String] = []
        for try await line in SSELineReader.lines(from: bytes) {
            result.append(line)
        }
        return result
    }

    @Test
    func crlfFramesKeepTheEmptyBoundaryLine() async throws {
        let text = "event: connected\r\ndata: {\"a\":1}\r\n\r\nevent: heartbeat\r\ndata: {}\r\n\r\n"
        #expect(try await lines(text) == ["event: connected", "data: {\"a\":1}", "", "event: heartbeat", "data: {}", ""])
    }

    @Test
    func lfAndBareCrFramesReadTheSame() async throws {
        let expected = ["event: a", "data: 1", "", "event: b", "data: 2", ""]
        #expect(try await lines("event: a\ndata: 1\n\nevent: b\ndata: 2\n\n") == expected)
        #expect(try await lines("event: a\rdata: 1\r\revent: b\rdata: 2\r\r") == expected)
    }

    @Test
    func trailingPartialLineIsDelivered() async throws {
        #expect(try await lines(": ping\r\n\r\ndata: tail") == [": ping", "", "data: tail"])
    }

    @Test
    func multiByteTextSurvivesByteSplitting() async throws {
        #expect(try await lines("data: héllo — 日本\r\n\r\n") == ["data: héllo — 日本", ""])
    }
}
