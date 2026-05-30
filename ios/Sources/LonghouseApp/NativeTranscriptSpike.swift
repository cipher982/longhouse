#if DEBUG
import SwiftUI

// MARK: - Phase 2 native-transcript SPIKE (decision gate — NOT shipping)
//
// A measured experiment, per the formal plan: can a native SwiftUI transcript
// handle hostile real agent data well enough to replace (all / live-tail-only /
// row-types-only) the WebKit transcript? This renders the REAL TimelineItem
// model natively and is driven by hostile #Preview fixtures (1000+ turns,
// 200-line bash output, dropped/orphaned tools, giant code blocks, long
// unbroken lines, markdown-in-JSON).
//
// Architecture follows 2026 best practice (researched):
//   • List (not raw LazyVStack) for row reuse across 1000+ rows.
//   • Per-block parsed-markdown cache keyed by content hash — never re-parse a
//     whole message on update; only the live tail block changes.
//   • .defaultScrollAnchor(.bottom) instead of hammering ScrollViewReader.
//   • Huge code blocks collapse by default; long lines scroll horizontally.
//   • Tool rows demoted to footnotes (matches the shipped redesign), dropped
//     results flagged in the shared attention color (preserve, never erase).
//
// This file is DEBUG-only and behind previews; it is not wired into the app.
//
// ─────────────────────────────────────────────────────────────────────────
// SPIKE FINDINGS (2026-05-30) — recorded with the code they describe.
//
// What worked natively, cleanly:
//   • TimelineItem maps 1:1 to native rows — user / assistant / tool / orphan /
//     passive — reusing the existing pairing model with zero new ingest logic.
//   • Demoted tool rows, dropped-result attention dot, collapse-large-output,
//     horizontal-scroll for long lines, and text selection all work and match
//     the shipped redesign. Tool expand/collapse is trivially native.
//   • Inline markdown (bold, code spans, links) renders via AttributedString.
//
// What broke / the real blockers:
//   • GFM BLOCKS are not free. AttributedString(markdown:) + Text does NOT
//     render fenced code blocks as blocks — the ```swift fence rendered as
//     literal text, and the 7-row "nasties" preview ballooned to a 10,584px
//     tall snapshot. Tables/task-lists are the same story. Confirms the
//     research: all-native needs Textual or swift-markdown + a custom block
//     renderer (code highlighting, tables), which is real, ongoing work.
//   • The 1200-turn preview FAILED to snapshot (harness renders full content
//     height, not a viewport). On-device List virtualization would mitigate
//     scroll, but it flags that unbounded block heights (giant code) are a
//     genuine memory/layout risk that WebKit currently absorbs for free.
//   • Streaming (coalesced batches + per-block cache + scroll-intent
//     controller) was NOT exercised here — it remains the largest unproven
//     piece and the most fragile (per Codex's review).
//
// RECOMMENDATION: do NOT go all-native now. The native path is viable for the
// CHROME-ADJACENT, low-markdown rows (tool rows, human messages, the live-tail
// status), but the assistant-prose block renderer (GFM + code highlighting +
// streaming + scroll-intent) is a multi-week build that WebKit already solves.
// Best value: KEEP WebKit for the transcript body; revisit "row-types-only"
// (native tool rows / live tail over a native list) only if a concrete user
// need (e.g. native selection, richer tool result cards) justifies adopting
// Textual + the streaming/caching harness. Phase 2 gate → STAY WEBKIT for now.
// ─────────────────────────────────────────────────────────────────────────

// MARK: Per-block markdown cache (the "don't reparse everything" rule)

@MainActor
final class MarkdownBlockCache {
    static let shared = MarkdownBlockCache()
    private var store: [Int: AttributedString] = [:]   // key: contentText hashValue

    func attributed(for text: String) -> AttributedString {
        let key = text.hashValue
        if let hit = store[key] { return hit }
        let opts = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible
        )
        let parsed = (try? AttributedString(markdown: text, options: opts)) ?? AttributedString(text)
        store[key] = parsed
        return parsed
    }
}

// MARK: Native rows

private struct NativeAssistantProse: View {
    let event: SessionEvent
    let isLive: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(isLive ? TranscriptPalette.live : Color.clear)
                .frame(width: 7, height: 7)
                .frame(width: 8)
                .padding(.top, 7)
                .accessibilityHidden(true)
            Text(MarkdownBlockCache.shared.attributed(for: event.contentText ?? ""))
                .font(.callout)
                .lineSpacing(4)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 8, leading: 18, bottom: 8, trailing: 18))
    }
}

private struct NativeHumanMessage: View {
    let event: SessionEvent

    var body: some View {
        HStack {
            Spacer(minLength: 56)
            Text(event.contentText ?? "")
                .font(.callout)
                .textSelection(.enabled)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color(.secondarySystemFill), in: RoundedRectangle(cornerRadius: 19, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 19, style: .continuous)
                        .strokeBorder(TranscriptPalette.live.opacity(0.30), lineWidth: 1)
                )
        }
        .listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 8, leading: 18, bottom: 8, trailing: 18))
    }
}

private struct NativeToolRow: View {
    let call: SessionEvent
    let result: SessionEvent?
    let pairing: ToolPairing
    @State private var expanded = false

    private var isDropped: Bool { call.toolCallState == .dropped || (result == nil && pairing != .pending) }
    private var isRunning: Bool { call.toolCallState == .running || pairing == .pending }

    // Big outputs collapse by default — the work is preserved, not erased.
    private var output: String { result?.toolOutputText ?? "" }
    private var isLargeOutput: Bool { output.count > 600 || output.filter { $0 == "\n" }.count > 8 }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 8) {
                    Image(systemName: toolIcon).font(.footnote).foregroundStyle(.secondary).frame(width: 16)
                    Text(call.toolName ?? "Tool").font(.footnote.weight(.semibold)).foregroundStyle(.primary)
                    Text(subtitle).font(.footnote.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 6)
                    trailing
                }
            }
            .buttonStyle(.plain)

            if expanded, !output.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {   // long lines scroll, never wrap-thrash
                    Text(output)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .padding(8)
                }
                .frame(maxHeight: isLargeOutput ? 220 : .infinity)
                .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .overlay(alignment: .leading) {
            Rectangle().fill(Color(.quaternaryLabel)).frame(width: 1.5).offset(x: -9)
        }
        .listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 2, leading: 34, bottom: 2, trailing: 18))
    }

    private var subtitle: String {
        if isLargeOutput && !expanded { return "\(output.filter { $0 == "\n" }.count + 1) lines · tap to expand" }
        return String((call.contentText ?? output).prefix(120))
    }

    @ViewBuilder private var trailing: some View {
        if isDropped {
            HStack(spacing: 4) {
                Circle().fill(TranscriptPalette.attention).frame(width: 5, height: 5)
                Text("no result").font(.caption2).foregroundStyle(.secondary)
            }
        } else if isRunning {
            Text("running").font(.caption2).foregroundStyle(.secondary)
        } else {
            Text("done").font(.caption2).foregroundStyle(.tertiary)
        }
    }

    private var toolIcon: String {
        switch (call.toolName ?? "").lowercased() {
        case let n where n.contains("bash") || n.contains("shell"): return "terminal"
        case let n where n.contains("read") || n.contains("explore"): return "magnifyingglass"
        case let n where n.contains("edit") || n.contains("write"): return "pencil"
        default: return "wrench.and.screwdriver"
        }
    }
}

// MARK: The native transcript (List-backed, anchored to bottom)

struct NativeTranscriptSpike: View {
    let items: [TimelineItem]
    var liveTailId: String? = nil

    var body: some View {
        List {
            ForEach(items) { item in
                row(for: item)
            }
        }
        .listStyle(.plain)
        .environment(\.defaultMinListRowHeight, 0)
        .scrollContentBackground(.hidden)
        .defaultScrollAnchor(.bottom)
        .background(Color(.systemBackground))
    }

    @ViewBuilder
    private func row(for item: TimelineItem) -> some View {
        switch item {
        case .user(let e):
            NativeHumanMessage(event: e)
        case .assistant(let e):
            NativeAssistantProse(event: e, isLive: item.id == liveTailId)
        case .tool(let call, let result, let pairing):
            NativeToolRow(call: call, result: result, pairing: pairing)
        case .orphanTool(let e):
            NativeToolRow(call: e, result: nil, pairing: .id)
        case .passiveGroup(let calls):
            ForEach(calls) { c in
                NativeToolRow(call: c.call, result: c.result, pairing: c.pairing)
            }
        }
    }
}

// MARK: Hostile fixtures

private enum SpikeFixture {
    static func event(
        _ id: Int, _ role: String, text: String? = nil,
        tool: String? = nil, output: String? = nil, callId: String? = nil,
        state: ToolCallState? = nil
    ) -> SessionEvent {
        SessionEvent(
            id: id, role: role, contentText: text, toolName: tool,
            toolInputJSON: nil, toolOutputText: output, toolCallId: callId,
            toolCallState: state, timestamp: "2026-05-30T10:00:\(String(format: "%02d", id % 60))Z",
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        )
    }

    // The specific nasties in one screen.
    static let nasties: [TimelineItem] = {
        let giantCode = "```swift\n" + String(repeating: "func longFunctionNameThatExercisesLayout\(Int.random(in: 0...9))() { let x = 42 }\n", count: 18) + "```"
        let longLine = String(repeating: "https://example.com/very/long/unbroken/path/segment/", count: 12)
        let mdInJson = "{\"summary\": \"## Heading\\n- item one\\n- item two\", \"code\": \"```py\\nprint('hi')\\n```\"}"
        let c1 = "k1"
        return [
            .user(event(1, "user", text: "Handle all the edge cases.")),
            .assistant(event(2, "assistant", text: "Here is a giant code block:\n\n\(giantCode)")),
            .assistant(event(3, "assistant", text: "A very long unbroken line: \(longLine)")),
            .tool(call: event(4, "assistant", text: "cat huge.log", tool: "Bash", callId: c1),
                  result: event(5, "tool", output: String(repeating: "log line with detail\n", count: 40), callId: c1), pairing: .id),
            .orphanTool(event(6, "assistant", tool: "getJiraIssue", callId: "orphan", state: .dropped)),
            .assistant(event(7, "assistant", text: "Markdown inside JSON: \(mdInJson)")),
            .tool(call: event(8, "assistant", tool: "Read", callId: "run1", state: .running), result: nil, pairing: .pending),
        ]
    }()
}

#Preview("Spike · hostile nasties · Dark") {
    NativeTranscriptSpike(items: SpikeFixture.nasties, liveTailId: "tool:8")
        .preferredColorScheme(.dark)
}

#Preview("Spike · hostile nasties · Light") {
    NativeTranscriptSpike(items: SpikeFixture.nasties)
        .preferredColorScheme(.light)
}

// NOTE: large-scale (150 / 1200-turn) previews were tried and FAILED the
// snapshot harness — it renders full content height (not a viewport), so even a
// modest List blows past its limits. That finding is recorded above; true
// scroll perf at 1000+ rows must be evaluated on-device with List
// virtualization, not via snapshots, so no large #Preview ships here.
#endif
