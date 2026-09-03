import Foundation
import Testing

@testable import Longhouse

/// The provider's turn accounting reaches the phone as a footer under the
/// row the turn ended on: "✻ Worked for 2m 9s · done 9:15 AM".
struct TurnEndCopyTests {
    private func event(_ id: Int, role: String, turnEnd: SessionTurnEnd? = nil) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: "\(role) \(id)",
            toolName: role == "tool" ? "Bash" : nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: role == "tool" ? "call-\(id)" : nil,
            toolCallState: nil,
            timestamp: "2026-09-03T14:20:39.100Z",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            turnEnd: turnEnd
        )
    }

    @Test func durationCompactsLikeTheTerminal() {
        #expect(TurnEndCopy.duration(milliseconds: 129_299) == "2m 9s")
        #expect(TurnEndCopy.duration(milliseconds: 58_459) == "58s")
        #expect(TurnEndCopy.duration(milliseconds: 49) == "0s")
        #expect(TurnEndCopy.duration(milliseconds: 780_000) == "13m")
        #expect(TurnEndCopy.duration(milliseconds: 3_720_000) == "1h 2m")
        #expect(TurnEndCopy.duration(milliseconds: 7_200_000) == "2h")
    }

    @Test func doneAtShowsOnlyTheClockOnTheSameDay() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let endedAt = "2026-09-03T14:20:39.100Z"
        let sameDay = calendar.date(from: DateComponents(year: 2026, month: 9, day: 3, hour: 18))!
        let laterInWeek = calendar.date(from: DateComponents(year: 2026, month: 9, day: 5, hour: 9))!
        let sameDayCopy = TurnEndCopy.doneAt(endedAt, now: sameDay, calendar: calendar)
        let weekCopy = TurnEndCopy.doneAt(endedAt, now: laterInWeek, calendar: calendar)
        #expect(sameDayCopy.hasPrefix("done "))
        #expect(sameDayCopy.contains("20"))
        #expect(weekCopy.hasPrefix("done "))
        #expect(weekCopy.count > sameDayCopy.count, "a turn from another day names the day")
        #expect(TurnEndCopy.doneAt("not a date") == "done")
    }

    @Test func footerAnchorsToTheRowThatCarriesTheStampedEvent() {
        let stamp = SessionTurnEnd(durationMs: 129_299, endedAt: "2026-09-03T14:20:39.100Z", messageCount: 898)
        let assistant = event(2, role: "assistant", turnEnd: stamp)
        let plain = event(3, role: "assistant")
        let call = event(4, role: "assistant")
        let result = event(5, role: "tool", turnEnd: stamp)

        let onAssistant = WebTranscriptView.turnEndPayload(for: .assistant(assistant), now: Date(timeIntervalSince1970: 1_788_000_000))
        #expect(onAssistant?.label == "Worked for 2m 9s")
        #expect(onAssistant?.doneAt.hasPrefix("done ") == true)
        #expect(WebTranscriptView.turnEndPayload(for: .assistant(plain)) == nil)

        // A turn that ends on a tool result decorates the tool row, and a
        // grouped run of tools takes the last stamped call in the group.
        #expect(WebTranscriptView.turnEndPayload(for: .tool(call: call, result: result, pairing: .id))?.label == "Worked for 2m 9s")
        let group: [ActivityCall] = [
            ActivityCall(call: event(6, role: "assistant"), result: nil, pairing: .id),
            ActivityCall(call: call, result: result, pairing: .id),
        ]
        #expect(WebTranscriptView.turnEndPayload(for: .activityGroup(calls: group))?.label == "Worked for 2m 9s")
    }

    @Test func usageChipCompactsTheModelLine() {
        let usage = SessionUsageLatest(model: "claude-opus-5", effort: "high", contextTokens: 501_447, outputTokens: 177, thinkingTokens: 0, at: "2026-09-03T14:18:40Z")
        #expect(usage.chipLabel == "opus 5 · high · 501k ctx")
        #expect(SessionUsageLatest(model: "openai/gpt-5.6-sol", effort: nil, contextTokens: 900, outputTokens: 1, thinkingTokens: nil, at: "").chipLabel == "gpt 5.6 sol · 900 ctx")
        #expect(SessionUsageLatest.compactTokens(1_260_000) == "1.3M")
        #expect(SessionUsageLatest.shortModelName("  ") == nil)
    }
}
