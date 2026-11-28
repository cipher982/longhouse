import SwiftUI

/// Collapsible agent checklist anchored inside the bottom control dock.
/// Collapsed by default to one calm row (`● 2/5 steps · <active step>`);
/// tapping expands the full list with bounded height and scrolling.
struct SessionChecklistDockView: View {
    let checklist: SessionChecklist?
    let sessionId: String

    @State private var expanded = false
    @Environment(\.dynamicTypeSize) private var typeSize

    private static var expandedBySession: [String: Bool] = [:]

    var body: some View {
        if let checklist, checklist.isVisibleInDock {
            VStack(alignment: .leading, spacing: 6) {
                Button {
                    expanded.toggle()
                    Self.expandedBySession[sessionId] = expanded
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: expanded ? "chevron.down" : "chevron.right")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Image(systemName: checklist.isComplete ? "checkmark.circle.fill" : "circle.inset.filled")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(checklist.isComplete ? Ember.sage : Ember.gold)
                        Text(checklist.countLabel)
                            .font(.caption.weight(.semibold))
                            .monospacedDigit()
                            .foregroundStyle(Ember.text)
                        if let detail = checklist.summaryDetail {
                            Text("· \(detail)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(typeSize.isAccessibilitySize ? 2 : 1)
                                .truncationMode(.tail)
                        }
                        Spacer(minLength: 0)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("session-checklist-toggle")
                .accessibilityLabel(accessibilitySummary(checklist))
                .accessibilityHint(expanded ? "Collapses the step checklist" : "Expands the step checklist")

                if expanded {
                    Divider()
                        .overlay(Ember.hairline)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 4) {
                            if let explanation = checklist.explanation, !explanation.isEmpty {
                                Text(explanation)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .padding(.bottom, 2)
                            }
                            ForEach(Array(checklist.items.enumerated()), id: \.offset) { index, item in
                                if let group = item.group, !group.isEmpty,
                                   index == 0 || checklist.items[index - 1].group != group {
                                    Text(group.uppercased())
                                        .font(.caption2.weight(.semibold))
                                        .foregroundStyle(.secondary)
                                        .padding(.top, index == 0 ? 0 : 4)
                                }
                                checklistRow(item)
                            }
                            if checklist.truncated {
                                Text("Additional steps truncated")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(maxHeight: 180)
                    .accessibilityIdentifier("session-checklist-list")
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Ember.raised.opacity(0.6))
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Ember.hairline, lineWidth: 0.75)
                    )
            )
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("session-checklist-strip")
            .onAppear {
                expanded = Self.expandedBySession[sessionId] ?? false
            }
            .onChange(of: sessionId) { _, nextId in
                expanded = Self.expandedBySession[nextId] ?? false
            }
        }
    }

    private func checklistRow(_ item: SessionChecklistItem) -> some View {
        let status = item.normalizedStatus
        let label = (status == .inProgress ? item.activeTitle : nil) ?? item.title
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: status.symbolName)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(status.tintColor)
                .frame(width: 12)
            Text(label)
                .font(.caption)
                .foregroundStyle(status.textColor)
                .strikethrough(status == .completed || status == .skipped, color: .secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func accessibilitySummary(_ checklist: SessionChecklist) -> String {
        if let detail = checklist.summaryDetail {
            return "Checklist \(checklist.countLabel), \(detail)"
        }
        return "Checklist \(checklist.countLabel)"
    }
}

private extension SessionChecklistItem.NormalizedStatus {
    var symbolName: String {
        switch self {
        case .completed: return "checkmark"
        case .inProgress: return "circle.fill"
        case .skipped: return "minus"
        case .failed: return "xmark"
        case .pending, .unknown: return "circle"
        }
    }

    var tintColor: Color {
        switch self {
        case .completed: return Ember.sage
        case .inProgress: return Ember.gold
        case .failed: return Ember.ember
        case .pending, .skipped, .unknown: return Ember.textMuted
        }
    }

    var textColor: Color {
        switch self {
        case .inProgress: return Ember.text
        case .completed, .skipped: return Ember.textMuted
        case .failed: return Ember.ember
        case .pending, .unknown: return Ember.textSecondary
        }
    }
}

#if DEBUG
#Preview("Checklist Dock — Dark") {
    VStack(spacing: 12) {
        SessionChecklistDockView(
            checklist: SessionChecklist(
                items: [
                    SessionChecklistItem(id: "1", title: "Kill livelocked git fetch", status: "completed", activeTitle: nil, group: nil),
                    SessionChecklistItem(id: "2", title: "Gate Kopia backup on battery", status: "in_progress", activeTitle: nil, group: nil),
                    SessionChecklistItem(id: "3", title: "Check Low Power Mode", status: "pending", activeTitle: nil, group: nil),
                ],
                counts: SessionChecklistCounts(total: 3, done: 1, inProgress: 1),
                currentTitle: "Gate Kopia backup on battery",
                updatedAt: "2026-11-28T16:22:25Z",
                providerTool: "update_steps",
                explanation: nil,
                truncated: false,
                state: "active"
            ),
            sessionId: "preview-dark"
        )
    }
    .padding()
    .background(Ember.card)
    .preferredColorScheme(.dark)
}

#Preview("Checklist Dock — Light") {
    VStack(spacing: 12) {
        SessionChecklistDockView(
            checklist: SessionChecklist(
                items: [
                    SessionChecklistItem(id: "1", title: "Kill livelocked git fetch", status: "completed", activeTitle: nil, group: nil),
                    SessionChecklistItem(id: "2", title: "Gate Kopia backup on battery", status: "completed", activeTitle: nil, group: nil),
                ],
                counts: SessionChecklistCounts(total: 2, done: 2, inProgress: 0),
                currentTitle: nil,
                updatedAt: "2026-11-28T16:24:24Z",
                providerTool: "update_steps",
                explanation: nil,
                truncated: false,
                state: "complete"
            ),
            sessionId: "preview-light"
        )
    }
    .padding()
    .background(Ember.card)
    .preferredColorScheme(.light)
}
#endif
