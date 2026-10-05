import SwiftUI

/// A timeline row's fire: the session's status as a small simulated fire
/// (`HearthEngine`). The cell is a placeholder in the row's layout; the fire
/// draws a little taller and wider than it (`HearthGeometry`), as light on
/// the card. Decoration only: the row's dot and status line carry the state.
struct TimelineHearthLamp: View {
    static let cell = CGSize(width: 34, height: 46)

    let session: SessionSummary
    let suppressed: Bool
    @Environment(\.colorScheme) private var colorScheme
    @State private var failed = !HearthEngine.shared.isAvailable

    var body: some View {
        let snapshot = HearthSnapshot(session: session, suppressed: suppressed, now: Date())
        let bounds = HearthGeometry(cell: Self.cell).bounds
        Color.clear
            .frame(width: Self.cell.width, height: Self.cell.height)
            .overlay(alignment: .topLeading) {
                if failed {
                    HearthGlyph(mode: snapshot.mode)
                } else {
                    HearthLayer(
                        key: session.threadId ?? session.id,
                        snapshot: snapshot,
                        lightBackground: colorScheme == .light,
                        onFailure: { failed = true }
                    )
                    .frame(width: bounds.width, height: bounds.height)
                    .offset(x: bounds.minX, y: bounds.minY)
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

private struct HearthLayer: UIViewRepresentable {
    let key: String
    let snapshot: HearthSnapshot
    let lightBackground: Bool
    let onFailure: () -> Void

    func makeUIView(context: Context) -> HearthLayerView {
        HearthLayerView()
    }

    func updateUIView(_ view: HearthLayerView, context: Context) {
        view.onFailure = onFailure
        view.apply(key: key, snapshot: snapshot, lightBackground: lightBackground)
    }
}

/// Static stand-in when Metal could not start.
private struct HearthGlyph: View {
    let mode: HearthMode

    var body: some View {
        Image(systemName: mode == .working || mode == .waiting ? "flame.fill" : "circle.hexagongrid.fill")
            .font(.system(size: mode == .waiting ? 13 : 17))
            .foregroundStyle(mode == .working || mode == .waiting ? Ember.flame : Ember.ash)
            .opacity(mode == .ended ? 0.4 : 0.75)
            .frame(width: TimelineHearthLamp.cell.width, height: TimelineHearthLamp.cell.height, alignment: .bottom)
    }
}

extension HearthSnapshot {
    /// The fire's inputs from a timeline card (web: hearthSnapshotFromSession).
    /// A suppressed or unknown card still shows its bed but never fires events.
    init(session: SessionSummary, suppressed: Bool, now: Date) {
        let signal = TimelineSignal.resolve(for: session, suppressed: suppressed, asOf: now)
        let mode: HearthMode
        switch signal {
        case .working: mode = .working
        case .attention: mode = .waiting
        case .closed: mode = .ended
        case .quiet, .unknown: mode = .idle
        }
        var subagents = 0
        var children: [String: ChildCounters] = [:]
        let facts = session.stateFacts
        if let delegation = facts.delegation,
           delegation.state == "pending", (delegation.count ?? 0) > 0,
           delegation.isValid(asOf: now) {
            subagents = max(0, delegation.kinds?["subagent"] ?? 0)
            for task in delegation.items ?? [] {
                guard task.kind == "subagent",
                      let childID = task.sessionId?.trimmingCharacters(in: .whitespacesAndNewlines),
                      !childID.isEmpty, childID != session.id else { continue }
                children[childID] = ChildCounters(
                    toolCalls: task.toolCalls,
                    assistantMessages: task.assistantMessages,
                    userMessages: task.userMessages
                )
            }
        }
        let toolName = facts.activityTool?.trimmingCharacters(in: .whitespacesAndNewlines)
        let liveTool = !suppressed && facts.activityState == "executing" && facts.activityEvidenceIsLive(asOf: now)
            && toolName?.isEmpty == false
        self.init(
            mode: mode,
            toolCalls: session.toolCalls ?? 0,
            assistantMessages: session.assistantMessages ?? 0,
            userMessages: session.userMessages ?? 0,
            subagents: subagents,
            children: children,
            tool: liveTool ? toolName : nil,
            lastActivity: session.lastActivityAt.flatMap(LonghouseDateParser.parse),
            started: session.startedAt.flatMap(LonghouseDateParser.parse)
        )
        acceptsEvents = !suppressed && signal != .unknown && mode != .ended
    }
}
