import Foundation
import SwiftUI

struct SessionDelegationTaskSheet: View {
    let facts: SessionDelegationFacts?
    let onOpenSubagent: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var sheetNow: Date

    init(
        facts: SessionDelegationFacts?,
        asOf: Date,
        onOpenSubagent: @escaping (String) -> Void
    ) {
        self.facts = facts
        self.onOpenSubagent = onOpenSubagent
        _sheetNow = State(initialValue: asOf)
    }

    private struct TaskGroup: Identifiable {
        let key: String
        let title: String
        let tasks: [SessionDelegationTask]

        var id: String { key }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    if let observationLine {
                        Text(observationLine)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.secondary)
                            .accessibilityIdentifier("session-runtime-background-observed")
                    }
                    content
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 18)
            }
            .background(Ember.page)
            .navigationTitle("Background work")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .task(id: freshnessTaskKey) {
            while !Task.isCancelled {
                let remaining = facts?.validUntil
                    .flatMap(LonghouseDateParser.parse)?
                    .timeIntervalSinceNow
                    ?? 30
                if remaining <= 0 {
                    sheetNow = Date()
                    return
                }
                let delay = min(30, max(1, remaining))
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                if !Task.isCancelled {
                    sheetNow = Date()
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private var freshnessTaskKey: String {
        "\(facts?.observedAt ?? ""):\(facts?.validUntil ?? "")"
    }

    private var observationLine: String? {
        guard let observedAt = facts?.observedAt,
              let date = LonghouseDateParser.parse(observedAt),
              let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) else {
            return nil
        }
        return "Observed \(age)"
    }

    @ViewBuilder
    private var content: some View {
        if let facts, facts.isValid(asOf: sheetNow), facts.state.lowercased() != "unknown" {
            if let items = facts.items {
                if items.isEmpty {
                    emptyState
                } else {
                    taskGroups(items)
                }
            } else {
                aggregateOnlyState(facts)
            }
        } else {
            unknownState
        }
        if let recent = facts?.recentItems, !recent.isEmpty {
            Text("Reported terminal history")
                .font(.headline)
            taskGroups(recent)
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("No active named background work", systemImage: "checkmark.circle")
                .font(.headline)
            Text("The provider reported no active tasks for this observation.")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityIdentifier("session-runtime-background-empty")
    }

    private func aggregateOnlyState(_ facts: SessionDelegationFacts) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Named task details unavailable", systemImage: "list.bullet.rectangle")
                .font(.headline)
            if let count = facts.count, count > 0 {
                Text("\(count) background \(count == 1 ? "task was" : "tasks were") reported, without provider task details.")
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("This observation contains aggregate background-work evidence only.")
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityIdentifier("session-runtime-background-aggregate-only")
    }

    private var unknownState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Background work status unknown", systemImage: "questionmark.circle")
                .font(.headline)
            Text("The provider's active-work evidence is missing or expired. No current membership is inferred from its absence.")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityIdentifier("session-runtime-background-unknown")
    }

    private func taskGroups(_ tasks: [SessionDelegationTask]) -> some View {
        ForEach(groups(for: tasks)) { group in
            VStack(alignment: .leading, spacing: 8) {
                Text(group.title)
                    .font(.headline)
                    .foregroundStyle(Ember.text)
                ForEach(group.tasks) { task in
                    taskRow(task)
                }
            }
        }
    }

    @ViewBuilder
    private func taskRow(_ task: SessionDelegationTask) -> some View {
        let title = taskTitle(task)
        let sessionId = task.sessionId?.trimmingCharacters(in: .whitespacesAndNewlines)
        let row = VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Ember.text)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if sessionId != nil {
                    Image(systemName: "arrow.up.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            Text("Status: \(task.status)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let timing = timingLine(for: task) {
                Text(timing)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let archive = archiveWorkLine(for: task) {
                Text("Archive · \(archive)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let progress = task.nativeProgress {
                Text("Provider progress · \(nativeProgressLine(progress))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Ember.card)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(Ember.border, lineWidth: 0.75)
        }
        if let sessionId {
            Button {
                onOpenSubagent(sessionId)
            } label: {
                row
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("session-runtime-background-task-\(task.id)")
            .accessibilityHint("Open the child transcript")
        } else {
            row
        }
    }

    private func groups(for tasks: [SessionDelegationTask]) -> [TaskGroup] {
        let order: [SessionDelegationCategory] = [.agents, .commands, .monitors, .other]
        let grouped = Dictionary(grouping: tasks) {
            SessionDelegationCategory(kind: $0.kind)
        }
        return order.compactMap { category in
            guard let tasks = grouped[category], !tasks.isEmpty else { return nil }
            return TaskGroup(key: category.rawValue, title: category.title, tasks: tasks)
        }
    }

    private func taskTitle(_ task: SessionDelegationTask) -> String {
        if let description = task.description {
            let trimmed = description.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
        }
        return readableKind(task.kind)
    }

    private func readableKind(_ rawKind: String) -> String {
        let kind = rawKind.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !kind.isEmpty else { return "Background task" }
        return kind.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func archiveWorkLine(for task: SessionDelegationTask) -> String? {
        guard let sessionId = task.sessionId, !sessionId.isEmpty else { return nil }
        var parts: [String] = []
        if let count = task.toolCalls { parts.append("\(count) \(count == 1 ? "tool call" : "tool calls")") }
        if let count = task.assistantMessages { parts.append("\(count) \(count == 1 ? "reply" : "replies")") }
        if let count = task.userMessages { parts.append("\(count) \(count == 1 ? "prompt" : "prompts")") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func nativeProgressLine(_ progress: SessionDelegationProgress) -> String {
        var parts: [String] = []
        if let observedAt = progress.observedAt,
           let date = LonghouseDateParser.parse(observedAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("observed \(age)")
        }
        if let tool = progress.currentTool { parts.append(tool) }
        if let intent = progress.lastIntent { parts.append(intent) }
        if let count = progress.toolCount { parts.append("\(count) tools") }
        if let count = progress.requests { parts.append("\(count) requests") }
        if let count = progress.tokens { parts.append("\(count) tokens") }
        if let duration = progress.durationMs { parts.append(String(format: "%.1fs observed", Double(duration) / 1000)) }
        return parts.joined(separator: " · ")
    }

    private func timingLine(for task: SessionDelegationTask) -> String? {
        var parts: [String] = []
        if let endedAt = task.endedAt,
           let date = LonghouseDateParser.parse(endedAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("Ended \(age)")
        }
        if let registeredAt = task.registeredAt,
           let date = LonghouseDateParser.parse(registeredAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("Registered \(age)")
        }
        if let startedAt = task.startedAt,
           let date = LonghouseDateParser.parse(startedAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("Started \(age)")
        } else if let firstObservedAt = task.firstObservedAt,
                  let date = LonghouseDateParser.parse(firstObservedAt),
                  let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("First observed \(age)")
        }
        if let lastActivityAt = task.lastActivityAt,
           let date = LonghouseDateParser.parse(lastActivityAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("Last activity \(age)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
