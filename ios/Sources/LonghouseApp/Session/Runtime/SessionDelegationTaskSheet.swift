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
    @State private var showDetails = false

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
                    if showDetails, let observationLine {
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
                ToolbarItem(placement: .topBarLeading) {
                    Button(showDetails ? "Compact" : "Details") {
                        showDetails.toggle()
                    }
                    .font(.subheadline)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .task(id: freshnessTaskKey) {
            if UITestHooks.holdsAmbientMotion {
                if let observed = facts?.observedAt.flatMap(LonghouseDateParser.parse) {
                    sheetNow = observed
                }
                return
            }
            let liveTick = hasActiveTasks && !reduceMotion
            while !Task.isCancelled {
                let remaining = facts?.validUntil
                    .flatMap(LonghouseDateParser.parse)?
                    .timeIntervalSinceNow
                    ?? 30
                if remaining <= 0 {
                    sheetNow = facts?.validUntil.flatMap(LonghouseDateParser.parse)
                        ?? facts?.observedAt.flatMap(LonghouseDateParser.parse)
                        ?? Date()
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

    private var effectiveNow: Date {
        if UITestHooks.holdsAmbientMotion,
           let observedAt = facts?.observedAt,
           let date = LonghouseDateParser.parse(observedAt) {
            return date
        }
        return sheetNow
    }

    private var observationLine: String? {
        guard let observedAt = facts?.observedAt,
              let date = LonghouseDateParser.parse(observedAt),
              let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) else {
            return nil
        }
        return "Observed \(age)"
    }

    @ViewBuilder
    private var content: some View {
        if let facts, facts.isValid(asOf: effectiveNow), facts.state.lowercased() != "unknown" {
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
        let taskStatus = effectiveStatus(for: task)
        let isWaiting = taskStatus.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "waiting"
        let displayStep = stepLine(for: task) ?? (isWaiting ? "Waiting on background work" : nil)
        let row = VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                taskStatusIndicator(for: taskStatus)
                if !typeSize.isAccessibilitySize && depth >= 2 {
                    Text("└─")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                Text(title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Ember.text)
                    .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if let duration = liveDurationLabel(for: task) {
                    Text(duration)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(task.endedAt == nil && !isWaiting ? Ember.nixie : .secondary)
                        .fixedSize(horizontal: true, vertical: false)
                }
                if sessionId != nil {
                    Image(systemName: "arrow.up.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            taskMetadataStrip(for: task)
            if let displayStep {
                Text(displayStep)
                    .font(.caption)
                    .foregroundStyle(Ember.text.opacity(0.86))
                    .lineLimit(showDetails || typeSize.isAccessibilitySize ? nil : 1)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if showDetails, let timing = timingLine(for: task) {
                Text(timing)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if showDetails, let archive = archiveWorkLine(for: task) {
                Text("Archive · \(archive)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if showDetails, let progress = task.nativeProgress {
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
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Ember.card)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Ember.border.opacity(0.65), lineWidth: 0.5)
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
        let taskStatus = effectiveStatus(for: task)
        let normalizedStatus = taskStatus.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let isFailure = normalizedStatus == "failed" || normalizedStatus == "error"
        let hasIdentity = isFailure
            || !(task.agentType?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
            || task.isFork == true
            || !(task.model?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
        let hasMetrics = (task.nativeProgress?.tokens ?? 0) > 0
            || (task.nativeProgress?.toolCount ?? 0) > 0

        let identityItems = Group {
            if isFailure {
                taskPill(
                    text: statusLabel(for: taskStatus),
                    foreground: statusDotColor(for: taskStatus),
                    border: statusDotColor(for: taskStatus).opacity(0.45)
                )
            }
            if let agentType = task.agentType?.trimmingCharacters(in: .whitespacesAndNewlines),
               !agentType.isEmpty {
                taskPill(text: agentType, foreground: Ember.textSecondary, border: Ember.border)
            }
            if task.isFork == true {
                taskPill(text: "fork", foreground: Ember.gold, border: Ember.gold.opacity(0.45))
            }
            if let model = task.model?.trimmingCharacters(in: .whitespacesAndNewlines),
               !model.isEmpty {
                Text(compactModelLabel(model))
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }

        let metricItems = Group {
            if let tokens = task.nativeProgress?.tokens, tokens > 0 {
                Text("↓ \(RuntimeElapsed.compactTokens(tokens))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            if let toolCount = task.nativeProgress?.toolCount, toolCount > 0 {
                Text("\(toolCount) \(toolCount == 1 ? "tool" : "tools")")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }

        if hasIdentity || hasMetrics {
            if typeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: 4) {
                    identityItems
                    metricItems
                }
            } else {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 6) {
                        identityItems
                        metricItems
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        if hasIdentity {
                            HStack(spacing: 6) { identityItems }
                        }
                        if hasMetrics {
                            HStack(spacing: 8) { metricItems }
                        }
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        identityItems
                        metricItems
                    }
                }
            }
        }
    }

    private func compactModelLabel(_ raw: String) -> String {
        var label = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if label.hasPrefix("claude-") {
            label = String(label.dropFirst("claude-".count))
        }
        let parts = label.split(separator: "-")
        if parts.count >= 3,
           let major = Int(parts[parts.count - 2]),
           let minor = Int(parts[parts.count - 1]) {
            let prefix = parts.dropLast(2).joined(separator: "-")
            return "\(prefix) \(major).\(minor)"
        }
        return label
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

    private func effectiveStatus(for task: SessionDelegationTask) -> String {
        let native = task.nativeProgress?.status?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return native.isEmpty ? task.status : native
    }

    @ViewBuilder
    private func taskStatusIndicator(for rawStatus: String) -> some View {
        let normalized = rawStatus.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized == "waiting" {
            Circle()
                .strokeBorder(Ember.signalQuiet, lineWidth: 1.5)
                .frame(width: 7, height: 7)
        } else {
            Circle()
                .fill(statusDotColor(for: normalized))
                .frame(width: 7, height: 7)
        }
    }

    private func statusDotColor(for rawStatus: String) -> Color {
        switch rawStatus.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "running", "watching":
            return Ember.flame
        case "waiting":
            return Ember.signalQuiet
        case "completed", "done":
            return Ember.signalLive
        case "failed", "error":
            return Ember.signalFault
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
            if let start = (task.startedAt ?? task.firstObservedAt).flatMap(LonghouseDateParser.parse),
               end >= start,
               end.timeIntervalSince(start) <= 86_400 {
                return RuntimeElapsed.label(from: start, to: end, precise: true)
            }
            if let durationMs = task.nativeProgress?.durationMs, durationMs > 0 {
                return RuntimeElapsed.label(seconds: Double(durationMs) / 1000.0, precise: true)
            }
            return nil
        }
        let validUntil = facts?.validUntil.flatMap(LonghouseDateParser.parse)
        let boundedNow = RuntimeElapsed.observedEnd(validUntil: validUntil, now: effectiveNow)
        if let start = (task.startedAt ?? task.firstObservedAt).flatMap(LonghouseDateParser.parse) {
            let interval = boundedNow.timeIntervalSince(start)
            if interval >= 0, interval <= 86_400 {
                return RuntimeElapsed.label(from: start, to: boundedNow, precise: true)
            }
            if let durationMs = task.nativeProgress?.durationMs, durationMs > 0 {
                return RuntimeElapsed.label(seconds: Double(durationMs) / 1000.0, precise: true)
            }
            if let anchoredEnd = (task.lastActivityAt ?? facts?.observedAt).flatMap(LonghouseDateParser.parse) {
                let anchoredInterval = anchoredEnd.timeIntervalSince(start)
                if anchoredInterval >= 0, anchoredInterval <= 86_400 {
                    return RuntimeElapsed.label(from: start, to: anchoredEnd, precise: true)
                }
            }
            return nil
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
           let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) {
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
           let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) {
            parts.append("Ended \(age)")
        }
        if let registeredAt = task.registeredAt,
           let date = LonghouseDateParser.parse(registeredAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) {
            parts.append("Registered \(age)")
        }
        if let startedAt = task.startedAt,
           let date = LonghouseDateParser.parse(startedAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) {
            parts.append("Started \(age)")
        } else if let firstObservedAt = task.firstObservedAt,
                  let date = LonghouseDateParser.parse(firstObservedAt),
                  let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) {
            parts.append("First observed \(age)")
        }
        if let lastActivityAt = task.lastActivityAt,
           let date = LonghouseDateParser.parse(lastActivityAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: effectiveNow) {
            parts.append("Last activity \(age)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
