import SwiftUI

// MARK: - Phase 1 redesign v2 — design mocks (preview-only)
//
// Design philosophy (corrected): COLOR IS SIGNAL, NEVER DECORATION.
// The menu-bar surface the maintainer prefers is monochrome type hierarchy with green
// appearing as exactly one thing — a small dot meaning "alive". We copy that
// discipline:
//   • Type is monochrome: primary / secondary / tertiary. No colored text.
//   • Green = one dot = live. Orange = one dot = needs-you. That's the palette.
//   • No sparkle/emoji-as-UI. Send is a clean monochrome circle.
//   • Hierarchy comes from size, weight, and whitespace — not paint.
//
// North star: a calm, trustworthy mission-control work document — not a chat
// app. Always legible: what happened, what's happening, will steering land.
//
// Honesty: only controls that map to real capabilities. No invented Stop.

// MARK: - Tokens

private enum LH {
    static let live = Color.green
    static let attention = Color.orange
    static let nodeSize: CGFloat = 7
    static let spineX: CGFloat = 14
}

// MARK: - Trust-state model (mirrors SessionDetail-derived fields)

private struct MockState {
    enum Tone { case running, idle, blocked, offline, ended }

    var tone: Tone
    var headline: String          // monochrome primary text
    var context: String           // "cinder", secondary — the machine/context
    var detail: String?           // optional secondary phrase
    var canSend: Bool
    var loopMode: SessionLoopMode
    var queuedNote: String?

    // The ONLY color in the chrome: a single state dot.
    var dot: Color {
        switch tone {
        case .running: return LH.live
        case .blocked: return LH.attention
        case .offline, .ended: return Color(.systemGray)
        case .idle: return Color(.systemGray2)
        }
    }
    var isExecuting: Bool { tone == .running }
}

private extension MockState {
    static let running = MockState(tone: .running, headline: "Working", context: "cinder",
        detail: "Parsing the changelog", canSend: true, loopMode: .assist, queuedNote: nil)
    static let idle = MockState(tone: .idle, headline: "Idle", context: "cinder",
        detail: "Waiting for next prompt", canSend: true, loopMode: .assist, queuedNote: nil)
    static let blocked = MockState(tone: .blocked, headline: "Needs you", context: "cinder",
        detail: "Awaiting permission to run tests", canSend: true, loopMode: .assist, queuedNote: nil)
    static let queued = MockState(tone: .running, headline: "Working", context: "cinder",
        detail: "Using Shell", canSend: true, loopMode: .assist,
        queuedNote: "1 queued")
    static let observeOnly = MockState(tone: .idle, headline: "Read only", context: "imported",
        detail: nil, canSend: false, loopMode: .manual, queuedNote: nil)
    static let offline = MockState(tone: .offline, headline: "cinder offline", context: "",
        detail: "no check-in for 4m", canSend: false, loopMode: .manual, queuedNote: nil)
    static let ended = MockState(tone: .ended, headline: "Ended", context: "",
        detail: "stopped by user · 12m ago", canSend: false, loopMode: .manual, queuedNote: nil)
}

// MARK: - Status line + composer (the control layer)

private struct StatusComposerSurface: View {
    let state: MockState
    @State private var text = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize

    // At accessibility sizes the fused row can't stay horizontal — it crushes
    // the chip + send into slivers (caught by the AX5 preview). Stack instead.
    private var isAccessibilitySize: Bool { typeSize.isAccessibilitySize }

    var body: some View {
        // ONE floating object: status fused to the composer's top edge.
        // No divider — spacing alone separates the two rows (less form-like).
        VStack(alignment: .leading, spacing: 6) {
            statusLine
            if state.canSend {
                composerRow
            } else {
                unavailableRow
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 24, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .strokeBorder(.white.opacity(0.10), lineWidth: 0.75)
                )
        )
        .shadow(color: .black.opacity(0.30), radius: 18, y: 6)
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
    }

    // One quiet monochrome line. The dot is the only color.
    private var statusLine: some View {
        HStack(spacing: 7) {
            indicator
            Text(state.headline)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
            if !state.context.isEmpty {
                Text(state.context)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            if let detail = state.detail, !isAccessibilitySize {
                Text("·").foregroundStyle(.tertiary)
                Text(detail)
                    .font(.subheadline)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            if let note = state.queuedNote {
                Text("·").foregroundStyle(.tertiary)
                Text(note)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            if state.canSend { loopChip }   // mode lives up here, not crowding the input
        }
        .padding(.horizontal, 4)
    }

    @ViewBuilder
    private var indicator: some View {
        ZStack {
            if state.isExecuting && !reduceMotion {
                // Breathing ring around the dot — motion, not color, signals live.
                Circle().stroke(state.dot.opacity(0.35), lineWidth: 1.5)
                    .frame(width: 13, height: 13)
            }
            Circle().fill(state.dot).frame(width: LH.nodeSize, height: LH.nodeSize)
        }
        .frame(width: 14, height: 14)
    }

    // Composer row inside the fused card. Input is a rounded field so the
    // tap target reads clearly against the card material.
    private var composerRow: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "plus")
                .font(.body.weight(.medium))
                .foregroundStyle(.secondary)
                .frame(width: 28, height: 28)

            Text(text.isEmpty ? "Message cinder…" : text)
                .font(.callout)
                .foregroundStyle(text.isEmpty ? .tertiary : .primary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color(.tertiarySystemFill), in: Capsule())

            sendButton
        }
    }

    // Monochrome send. Light circle + dark arrow when active; ghost when empty.
    private var sendButton: some View {
        Image(systemName: "arrow.up")
            .font(.subheadline.weight(.bold))
            .foregroundStyle(text.isEmpty ? Color(.systemGray) : Color.black)
            .frame(width: 30, height: 30)
            .background(
                Circle().fill(text.isEmpty
                    ? AnyShapeStyle(Color(.tertiarySystemFill))
                    : AnyShapeStyle(Color.primary))
            )
    }

    // Loop mode: tiny monochrome chip. Icon-only at accessibility sizes so it
    // never crushes the status line. Not a width-eating strip, not colored.
    private var loopChip: some View {
        HStack(spacing: 3) {
            Image(systemName: loopIcon).font(.caption2.weight(.semibold))
            if !isAccessibilitySize {
                Text(loopLabel).font(.caption2.weight(.medium))
            }
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color(.quaternarySystemFill)))
    }

    private var unavailableRow: some View {
        HStack(spacing: 10) {
            Image(systemName: state.tone == .offline ? "wifi.slash" : (state.tone == .ended ? "checkmark.seal" : "eye"))
                .font(.body)
                .foregroundStyle(state.tone == .offline ? LH.attention : .secondary)
            Text(unavailableMessage)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
    }

    private var unavailableMessage: String {
        switch state.tone {
        case .offline: return "Steering unavailable until cinder reconnects"
        case .ended: return "Session ended — history is read-only"
        default: return "Observe-only from here"
        }
    }

    private var loopIcon: String {
        switch state.loopMode {
        case .assist: return "wand.and.stars"
        case .autopilot: return "bolt"
        case .manual: return "pause"
        }
    }
    private var loopLabel: String {
        switch state.loopMode {
        case .assist: return "Assist"
        case .autopilot: return "Auto"
        case .manual: return "Off"
        }
    }
}

// MARK: - Transcript column

private struct ToolRowMock: View {
    let icon: String
    let name: String
    let subtitle: String
    let timing: String
    var importance: Importance = .normal
    var resultMissing: Bool = false

    enum Importance { case normal, work }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(width: 16)
            Text(name)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.primary)
            Text(subtitle)
                .font(.footnote.monospaced())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 6)
            if resultMissing {
                // Attention is a dot, not colored text.
                Circle().fill(LH.attention).frame(width: 5, height: 5)
                Text("no result").font(.caption2).foregroundStyle(.secondary)
            } else {
                Text(timing).font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 5)
        .overlay(alignment: .leading) {
            if importance == .work {
                Capsule().fill(LH.attention.opacity(0.8)).frame(width: 2.5).offset(x: -9)
            }
        }
    }
}

private struct TranscriptColumnMock: View {
    let state: MockState

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            assistantProse(
                "Now I can see exactly what Alex did after the meeting. Two new blocker tickets appeared: **PROJ-101** and **PROJ-102**. Let me pull those and the new description text he wrote.",
                running: false
            )
            toolGroup
            humanMessage("Also check the MR state — who renamed it and when.")
            assistantProse(
                state.isExecuting
                    ? "Parsing the changelog for the rename moves…"
                    : "The MR was renamed by Alex at 18:42, then moved back to In Review.",
                running: state.isExecuting
            )
        }
        .padding(.horizontal, 18)
        .padding(.top, 10)
    }

    // Prose is a plain document paragraph. A running turn gets a small leading
    // dot — the ONLY liveness marker (principle 3: no decorative spine).
    private func assistantProse(_ text: String, running: Bool) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(running ? LH.live : Color.clear)
                .frame(width: LH.nodeSize, height: LH.nodeSize)
                .frame(width: 8)
                .padding(.top, 7)
                .accessibilityHidden(true)
            Text(.init(text))
                .font(.callout)
                .lineSpacing(4)
                .foregroundStyle(.primary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // Tool events: indented as a group (grouping is real structure, not decor).
    // No rail. Indent + tighter type does the grouping work and survives a11y.
    private var toolGroup: some View {
        VStack(alignment: .leading, spacing: 3) {
            ToolRowMock(icon: "arrow.triangle.branch", name: "getJiraIssue", subtitle: "PROJ-101", timing: "2.3s")
            ToolRowMock(icon: "terminal", name: "Bash", subtitle: "git log --oneline ~/.claude/projects", timing: "1.1s")
            ToolRowMock(icon: "terminal", name: "Bash", subtitle: "pytest tests/ — 3 failed, 41 passed", timing: "8.4s", importance: .work)
            ToolRowMock(icon: "magnifyingglass", name: "Explored", subtitle: "mcp__atlassian__getJiraIssue", timing: "", resultMissing: true)
        }
        .padding(.leading, 16)
    }

    private func humanMessage(_ text: String) -> some View {
        HStack {
            Spacer(minLength: 56)
            Text(text)
                .font(.callout)
                .foregroundStyle(.primary)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color(.secondarySystemFill), in: RoundedRectangle(cornerRadius: 19, style: .continuous))
        }
    }
}

// MARK: - Full screen

private struct RedesignScreenMock: View {
    let state: MockState

    var body: some View {
        VStack(spacing: 0) {
            navBar
            ScrollView {
                TranscriptColumnMock(state: state).padding(.bottom, 16)
            }
            StatusComposerSurface(state: state)
        }
        .background(Color(.systemBackground))
    }

    private var navBar: some View {
        HStack(spacing: 12) {
            Image(systemName: "chevron.left").font(.body.weight(.semibold)).foregroundStyle(.primary)
                .frame(width: 34, height: 34)
                .background(.ultraThinMaterial, in: Circle())
            VStack(alignment: .leading, spacing: 1) {
                Text("Post-Meeting Ticket Rename").font(.headline).lineLimit(1)
                Text("zerg · main").font(.caption2).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "bell").font(.body).foregroundStyle(.primary)
                .frame(width: 34, height: 34)
                .background(.ultraThinMaterial, in: Circle())
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

// MARK: - Previews

#Preview("v2 · running · Dark") { RedesignScreenMock(state: .running).preferredColorScheme(.dark) }
#Preview("v2 · running · Light") { RedesignScreenMock(state: .running).preferredColorScheme(.light) }
#Preview("v2 · idle · Dark") { RedesignScreenMock(state: .idle).preferredColorScheme(.dark) }
#Preview("v2 · needs-you · Dark") { RedesignScreenMock(state: .blocked).preferredColorScheme(.dark) }
#Preview("v2 · queued · Dark") { RedesignScreenMock(state: .queued).preferredColorScheme(.dark) }
#Preview("v2 · observe-only · Dark") { RedesignScreenMock(state: .observeOnly).preferredColorScheme(.dark) }
#Preview("v2 · offline · Dark") { RedesignScreenMock(state: .offline).preferredColorScheme(.dark) }
#Preview("v2 · ended · Dark") { RedesignScreenMock(state: .ended).preferredColorScheme(.dark) }

#Preview("v2 surface · running · Dark") {
    VStack { Spacer(); StatusComposerSurface(state: .running) }
        .background(Color(.systemBackground)).preferredColorScheme(.dark)
}
#Preview("v2 surface · needs-you · Dark") {
    VStack { Spacer(); StatusComposerSurface(state: .blocked) }
        .background(Color(.systemBackground)).preferredColorScheme(.dark)
}

// Accessibility gate (principle 3): AX5 Dynamic Type.
// If the layout holds and meaning survives here, the no-spine call was right.
// (Reduce Motion is read-only in the environment; the mock reads it at runtime
// and simply omits the breathing ring — nothing to force in a static snapshot.)
#Preview("v2 · AX5 Dynamic Type · Dark") {
    RedesignScreenMock(state: .running)
        .environment(\.dynamicTypeSize, .accessibility5)
        .preferredColorScheme(.dark)
}
