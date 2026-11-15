import Foundation
import SwiftUI

struct SessionDelegationTaskSheet: View {
    let facts: SessionDelegationFacts?
    let pendingBackgroundAgentCount: Int?
    let onOpenSubagent: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var sheetNow: Date

    init(
        facts: SessionDelegationFacts?,
        pendingBackgroundAgentCount: Int? = nil,
        asOf: Date,
        onOpenSubagent: @escaping (String) -> Void
    ) {
        self.facts = facts
        self.pendingBackgroundAgentCount = pendingBackgroundAgentCount
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
            let liveTick = hasActiveTasks && !UITestHooks.holdsAmbientMotion && !reduceMotion
            while !Task.isCancelled {
                let remaining = facts?.validUntil
                    .flatMap(LonghouseDateParser.parse)?
                    .timeIntervalSinceNow
                    ?? 30
                if remaining <= 0 {
                    sheetNow = Date()
                    return
                }
                let maxStep: TimeInterval = liveTick ? 1 : 30
                let delay = min(maxStep, max(1, remaining))
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                if !Task.isCancelled {
                    sheetNow = Date()
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private var hasActiveTasks: Bool {
        facts?.items?.contains(where: { ($0.endedAt ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) == true
    }

    private var freshnessTaskKey: String {
        "\(facts?.observedAt ?? ""):\(facts?.validUntil ?? ""):\(hasActiveTasks):\(pendingBackgroundAgentCount ?? 0)"
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
                    if let pending = pendingBackgroundAgentCount, pending > 0 {
                        waitingBackgroundAgentsCard(count: pending)
                    } else {
                        emptyState
                    }
                } else {
                    taskGroups(items)
                }
            } else {
                if let pending = pendingBackgroundAgentCount, pending > 0 {
                    waitingBackgroundAgentsCard(count: pending)
                }
                aggregateOnlyState(facts)
            }
        } else if facts == nil, let pending = pendingBackgroundAgentCount, pending > 0 {
            waitingBackgroundAgentsCard(count: pending)
        } else {
            unknownState
        }
        if let recent = facts?.recentItems, !recent.isEmpty {
            Text("Reported terminal history")
                .font(.headline)
            taskGroups(recent)
        }
    }

    private func waitingBackgroundAgentsCard(count: Int) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(
                "Waiting for \(count) background \(count == 1 ? "agent" : "agents") to finish",
                systemImage: "hourglass"
            )
            .font(.headline)
            .foregroundStyle(Ember.text)
            Text("Claude reported pending background work at the end of the turn.")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityIdentifier("session-runtime-background-waiting")
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
        let depth = max(1, task.spawnDepth ?? 1)
        let leadingIndent: CGFloat = (!typeSize.isAccessibilitySize && depth >= 2)
            ? CGFloat(min(depth - 1, 3)) * 12
            : 0
        let row = VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Circle()
                    .fill(statusDotColor(for: task.status))
                    .frame(width: 7, height: 7)
                Text(title)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Ember.text)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if let duration = liveDurationLabel(for: task) {
                    Text(duration)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(task.endedAt == nil ? Ember.nixie : .secondary)
                        .fixedSize(horizontal: true, vertical: false)
                }
                if sessionId != nil {
                    Image(systemName: "arrow.up.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            taskMetadataStrip(for: task)
            if let step = stepLine(for: task) {
                Text(step)
                    .font(.caption)
                    .foregroundStyle(Ember.text.opacity(0.88))
                    .fixedSize(horizontal: false, vertical: true)
            }
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
                let statsLine = nativeProgressLine(progress)
                if !statsLine.isEmpty {
                    Text(statsLine)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
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
        .padding(.leading, leadingIndent)

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

    @ViewBuilder
    private func taskMetadataStrip(for task: SessionDelegationTask) -> some View {
        let content = Group {
            taskPill(
                text: statusLabel(for: task.status),
                foreground: statusDotColor(for: task.status),
                border: statusDotColor(for: task.status).opacity(0.45)
            )
            if let agentType = task.agentType?.trimmingCharacters(in: .whitespacesAndNewlines),
               !agentType.isEmpty {
                taskPill(text: agentType, foreground: Ember.textSecondary, border: Ember.border)
            }
            if task.isFork == true {
                taskPill(text: "fork", foreground: Ember.gold, border: Ember.gold.opacity(0.45))
            }
            if let model = task.model?.trimmingCharacters(in: .whitespacesAndNewlines),
               !model.isEmpty {
                Text(model)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            if let tokens = task.nativeProgress?.tokens, tokens > 0 {
                Text(RuntimeElapsed.compactTokens(tokens))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        if typeSize.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 4) { content }
        } else {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) { content }
                VStack(alignment: .leading, spacing: 4) { content }
            }
        }
    }

    private func taskPill(text: String, foreground: Color, border: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .foregroundStyle(foreground)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Ember.well.opacity(0.75)))
            .overlay(Capsule().stroke(border, lineWidth: 0.75))
    }

    private func statusDotColor(for rawStatus: String) -> Color {
        switch rawStatus.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "running", "watching":
            return Ember.flame
        case "waiting":
            return Ember.gold
        case "failed", "error":
            return Ember.ember
        default:
            return Ember.ash
        }
    }

    private func statusLabel(for rawStatus: String) -> String {
        let trimmed = rawStatus.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "Unknown" }
        return trimmed.prefix(1).uppercased() + trimmed.dropFirst().lowercased()
    }

    private func liveDurationLabel(for task: SessionDelegationTask) -> String? {
        if let endedAt = task.endedAt, let end = LonghouseDateParser.parse(endedAt) {
            if let start = (task.startedAt ?? task.firstObservedAt).flatMap(LonghouseDateParser.parse) {
                return RuntimeElapsed.label(from: start, to: end, precise: true)
            }
            if let durationMs = task.nativeProgress?.durationMs, durationMs > 0 {
                return RuntimeElapsed.label(seconds: Double(durationMs) / 1000.0, precise: true)
            }
            return nil
        }
        if let start = (task.startedAt ?? task.firstObservedAt).flatMap(LonghouseDateParser.parse) {
            return RuntimeElapsed.label(from: start, to: sheetNow, precise: true)
        }
        if let durationMs = task.nativeProgress?.durationMs, durationMs > 0 {
            return RuntimeElapsed.label(seconds: Double(durationMs) / 1000.0, precise: true)
        }
        return nil
    }

    private func stepLine(for task: SessionDelegationTask) -> String? {
        guard let progress = task.nativeProgress else { return nil }
        let tool = progress.currentTool?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let intent = progress.lastIntent?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        switch (!tool.isEmpty, !intent.isEmpty) {
        case (true, true): return "\(tool): \(intent)"
        case (true, false): return tool
        case (false, true): return intent
        case (false, false): return nil
        }
    }

    private func groups(for tasks: [SessionDelegationTask]) -> [TaskGroup] {
        let order: [SessionDelegationCategory] = [.agents, .commands, .monitors, .other]
        let grouped = Dictionary(grouping: tasks) {
            SessionDelegationCategory(kind: $0.kind)
        }
        return order.compactMap { category in
            guard let categoryTasks = grouped[category], !categoryTasks.isEmpty else { return nil }
            let ordered = category == .agents ? orderedAgentTasks(categoryTasks) : categoryTasks
            return TaskGroup(key: category.rawValue, title: category.title, tasks: ordered)
        }
    }

    private func orderedAgentTasks(_ tasks: [SessionDelegationTask]) -> [SessionDelegationTask] {
        let ids = Set(tasks.map(\.id))
        let childrenByParent = Dictionary(grouping: tasks.filter { task in
            guard let parent = task.parentAgentId else { return false }
            return parent != task.id && ids.contains(parent)
        }) { $0.parentAgentId ?? "" }
        var result: [SessionDelegationTask] = []
        var visited = Set<String>()

        func appendWithChildren(_ task: SessionDelegationTask) {
            guard visited.insert(task.id).inserted else { return }
            result.append(task)
            for child in childrenByParent[task.id] ?? [] {
                appendWithChildren(child)
            }
        }

        for task in tasks {
            let parent = task.parentAgentId
            if parent == nil || parent == task.id || !ids.contains(parent!) {
                appendWithChildren(task)
            }
        }
        for task in tasks where !visited.contains(task.id) {
            appendWithChildren(task)
        }
        return result
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
        if let count = progress.toolCount { parts.append("\(count) tools") }
        if let count = progress.requests { parts.append("\(count) requests") }
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
