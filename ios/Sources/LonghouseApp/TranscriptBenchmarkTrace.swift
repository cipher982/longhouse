#if DEBUG
import Foundation

enum TranscriptBenchmarkRendererKind: String, CaseIterable, Sendable {
    case snapshotWebKit = "snapshot-webkit"
    case retainedWebKit = "retained-webkit"
    case nativeUIKit = "native-uikit"

    var semanticTier: String {
        switch self {
        case .snapshotWebKit: return "production"
        case .retainedWebKit, .nativeUIKit: return "mechanical-lower-bound"
        }
    }

    /// Candidate names are reserved in the result schema now so later spikes
    /// cannot quietly change methodology. Only a real implementation may flip
    /// its availability bit.
    var isImplemented: Bool {
        self == .snapshotWebKit
    }

    static var selected: TranscriptBenchmarkRendererKind {
        guard let raw = UITestHooks.transcriptBenchmarkRenderer,
              let renderer = TranscriptBenchmarkRendererKind(rawValue: raw) else {
            return .snapshotWebKit
        }
        return renderer
    }
}

/// Deterministic renderer workload shared by simulator regression tests and
/// opt-in physical-device benchmark runs. This is intentionally data-only: a
/// renderer may not weaken or reshape the trace to make its numbers look good.
enum TranscriptBenchmarkTrace {
    static let schemaVersion = 1
    static let traceName = "agent-core-v1"
    static let initialRowCount = 120
    static let streamingUpdateCount = 120
    static let streamingIntervalNanoseconds: UInt64 = 50_000_000
    static let prependRowCount = 50

    static let implementedOperations = [
        "initial_120_rows",
        "stream_12000_chars_20hz",
        "three_tool_transitions",
        "prepend_50_rows",
        "scroll_away_during_stream",
        "composer_focus_during_stream",
    ]

    static let deferredOperations = [
        "delayed_media_resize",
        "tool_disclosure_expansion",
        "collapsed_message_expansion",
        "measured_prepend_anchor_error",
    ]

    static func initialEvents() -> [SessionEvent] {
        (0..<initialRowCount).map { index in
            let role = index.isMultiple(of: 2) ? "user" : "assistant"
            return messageEvent(
                id: index + 1,
                role: role,
                content: initialMessage(index: index, role: role),
                timestampOffset: index
            )
        }
    }

    static func streamingSnapshots() -> [String] {
        let source = Array(streamingDocument.utf8)
        return (1...streamingUpdateCount).map { update in
            var length = min(
                source.count,
                Int(ceil(Double(source.count * update) / Double(streamingUpdateCount)))
            )
            while length > 0,
                  String(bytes: source.prefix(length), encoding: .utf8) == nil {
                length -= 1
            }
            return String(bytes: source.prefix(length), encoding: .utf8) ?? ""
        }
    }

    static func olderEvents() -> [SessionEvent] {
        (0..<prependRowCount).map { index in
            messageEvent(
                id: "benchmark-older-\(index)",
                role: index.isMultiple(of: 2) ? "user" : "assistant",
                content: "Older benchmark row \(index): preserved above the visible anchor during pagination.",
                timestampOffset: -prependRowCount + index
            )
        }
    }

    static func messageEvent(
        id: Int,
        role: String,
        content: String,
        timestampOffset: Int
    ) -> SessionEvent {
        messageEvent(id: String(id), role: role, content: content, timestampOffset: timestampOffset)
    }

    static func messageEvent(
        id: String,
        role: String,
        content: String,
        timestampOffset: Int
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp(offset: timestampOffset),
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    static func toolCallEvent(
        id: Int,
        callID: String,
        ordinal: Int,
        state: ToolCallState
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "assistant",
            contentText: nil,
            toolName: ordinal.isMultiple(of: 2) ? "Bash" : "Read",
            toolInputJSON: ["command": .string("benchmark tool \(ordinal)")],
            toolOutputText: nil,
            toolCallId: callID,
            toolCallState: state,
            timestamp: timestamp(offset: initialRowCount + id),
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    static func toolResultEvent(id: Int, callID: String, ordinal: Int) -> SessionEvent {
        SessionEvent(
            id: id,
            role: "tool",
            contentText: nil,
            toolName: ordinal.isMultiple(of: 2) ? "Bash" : "Read",
            toolInputJSON: nil,
            toolOutputText: "Benchmark tool \(ordinal) completed.\n" + String(repeating: "output line \(ordinal)\n", count: 20),
            toolCallId: callID,
            toolCallState: .completed,
            timestamp: timestamp(offset: initialRowCount + id),
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil
        )
    }

    private static func initialMessage(index: Int, role: String) -> String {
        if role == "user" {
            return "Benchmark prompt \(index): inspect the renderer, preserve the scroll anchor, and report concrete evidence."
        }
        switch index % 12 {
        case 1:
            return "## Renderer checkpoint \(index)\n\nThis response contains **emphasis**, `inline code`, a [link](https://longhouse.ai), and Unicode: λ 東京 🚀."
        case 3:
            return "- Preserve stable identities\n- Update only changed content\n  - Keep nested structure readable\n- Never steal the user's scroll position"
        case 5:
            return "```swift\nlet revision = \(index)\nrenderer.apply(.append(id: \"assistant\", delta: \"token\"))\n```"
        case 7:
            return "| Stage | Budget |\n|:--|--:|\n| Parse | 2 ms |\n| Layout | 5 ms |\n| Composite | 8 ms |"
        default:
            return "Assistant benchmark row \(index). The settled rows should do no work while the active response grows."
        }
    }

    private static let streamingDocument: String = {
        let unit = """
        ## Streaming renderer analysis

        The active response grows at twenty updates per second. Settled blocks should remain settled, selection should remain responsive, and scrolling should not jump.

        - Parse only the unfinished block.
        - Preserve stable row identity.
        - Keep `code`, **emphasis**, [links](https://longhouse.ai), and Unicode λ 東京 🚀 intact.

        ```swift
        renderer.apply(.append(itemID: "streaming-assistant", delta: nextChunk))
        ```

        | Metric | Target |
        |:--|--:|
        | Update to visible | 16.7 ms |
        | Anchor error | 2 pt |
        | Main-thread stalls | 0 |

        """
        var document = ""
        while document.utf8.count < 12_000 {
            document += unit
        }
        return String(decoding: document.utf8.prefix(12_000), as: UTF8.self)
    }()

    private static func timestamp(offset: Int) -> String {
        let base = Date(timeIntervalSince1970: 1_788_880_000)
        return ISO8601DateFormatter().string(from: base.addingTimeInterval(TimeInterval(offset)))
    }
}

struct TranscriptBenchmarkTraceResult: Sendable {
    let updateCount: Int
    let expectedLatestItemID: String
}
#endif
