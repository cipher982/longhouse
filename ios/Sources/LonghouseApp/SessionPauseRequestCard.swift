import SwiftUI

struct SessionAttentionFallbackCard: View {
    let detail: SessionDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().opacity(0.4)

            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "exclamationmark.bubble")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
                    .frame(width: 18, height: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Needs attention")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(detail.runtimeHeadline)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                    Text(detail.runtimeDetail ?? "Check the original terminal for the next step.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
                Spacer(minLength: 0)
            }

            Label("Waiting in terminal", systemImage: "terminal")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("session-attention-fallback")
    }
}

// Per-page natural content height, keyed by question index, so the pause card
// can hug short questions and only scroll when one genuinely overflows.
private struct PauseQuestionHeightKey: PreferenceKey {
    static let defaultValue: [Int: CGFloat] = [:]
    static func reduce(value: inout [Int: CGFloat], nextValue: () -> [Int: CGFloat]) {
        value.merge(nextValue(), uniquingKeysWith: { max($0, $1) })
    }
}

struct SessionPauseRequestCard: View {
    let pauseRequest: SessionPauseRequest
    let isResponding: Bool
    let errorMessage: String?
    let onRespond: (
        _ decision: String,
        _ answers: [String: [String]]?,
        _ content: String?,
        _ message: String?
    ) async -> Bool

    @State private var answers: [String: [String]]
    @State private var fallbackText: String
    @State private var submitted = false
    @State private var currentPage = 0
    @State private var measuredHeights: [Int: CGFloat] = [:]

    init(
        pauseRequest: SessionPauseRequest,
        isResponding: Bool,
        errorMessage: String?,
        onRespond: @escaping (
            _ decision: String,
            _ answers: [String: [String]]?,
            _ content: String?,
            _ message: String?
        ) async -> Bool
    ) {
        self.pauseRequest = pauseRequest
        self.isResponding = isResponding
        self.errorMessage = errorMessage
        self.onRespond = onRespond
        _answers = State(initialValue: Self.initialAnswers(for: pauseRequest.questions))
        _fallbackText = State(initialValue: "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().opacity(0.4)

            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "questionmark.bubble")
                    .font(.subheadline)
                    .foregroundStyle(.orange)
                    .frame(width: 18, height: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text(isPermissionPrompt ? "Permission" : "Needs answer")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(pauseRequest.title?.nonEmptyTrimmed ?? (isPermissionPrompt ? "Tool permission" : "Provider question"))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                    Text(detailText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
                Spacer(minLength: 0)
                if pageCount > 1 {
                    pageDots
                }
            }

            if pauseRequest.questions.isEmpty && !isPermissionPrompt {
                if pauseRequest.canRespond {
                    TextField("Answer", text: $fallbackText, axis: .vertical)
                        .lineLimit(1...4)
                        .disabled(isDisabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .accessibilityIdentifier("session-pause-freeform")
                }
            } else if pageCount > 1 {
                // One question per page. Each page scrolls only when its content
                // is taller than the cap; otherwise the region hugs the content
                // so there's no dead space between the question and the footer.
                TabView(selection: $currentPage) {
                    ForEach(Array(pauseRequest.questions.enumerated()), id: \.offset) { index, question in
                        questionPage(question: question, index: index)
                            .tag(index)
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .frame(height: resolvedPageHeight)
                .animation(.easeInOut(duration: 0.22), value: currentPage)
                .animation(.easeInOut(duration: 0.22), value: resolvedPageHeight)
                .accessibilityIdentifier("session-pause-pager")
            } else if let question = pauseRequest.questions.first {
                questionPage(question: question, index: 0)
                    .frame(height: resolvedPageHeight)
            }

            if let errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("session-pause-error")
            }

            footer
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("session-pause-card")
        .onChange(of: pauseRequest.id) { _, _ in
            answers = Self.initialAnswers(for: pauseRequest.questions)
            fallbackText = ""
            submitted = false
            currentPage = 0
            // Drop stale heights so a new, shorter request doesn't briefly
            // inherit the previous card's height (reintroducing dead space).
            measuredHeights = [:]
        }
    }

    private var pageCount: Int { pauseRequest.questions.count }

    private var isLastPage: Bool { currentPage >= pageCount - 1 }

    // Upper bound so the pinned footer stays on screen regardless of how many
    // long-description options a single question carries. Below this the region
    // hugs the measured content height instead of reserving the full cap.
    private var pageMaxHeight: CGFloat { 340 }

    // Size the question region to the current page's actual content, clamped to
    // the cap. Falls back to the cap until the page reports its height so the
    // footer never jumps off-screen on first layout.
    private var resolvedPageHeight: CGFloat {
        guard let measured = measuredHeights[currentPage] else { return pageMaxHeight }
        return min(measured, pageMaxHeight)
    }

    // A single question, scrollable only when it overflows the cap, reporting
    // its natural content height back up for the hug-to-content sizing.
    @ViewBuilder
    private func questionPage(question: SessionPauseQuestion, index: Int) -> some View {
        ScrollView {
            questionView(question: question, index: index)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    GeometryReader { proxy in
                        Color.clear.preference(
                            key: PauseQuestionHeightKey.self,
                            value: [index: proxy.size.height]
                        )
                    }
                )
        }
        .scrollBounceBehavior(.basedOnSize)
        .onPreferenceChange(PauseQuestionHeightKey.self) { heights in
            for (key, value) in heights {
                measuredHeights[key] = value
            }
        }
    }

    private var pageDots: some View {
        HStack(spacing: 5) {
            ForEach(0..<pageCount, id: \.self) { index in
                Circle()
                    .fill(index == currentPage ? Color.accentColor : Color.secondary.opacity(0.4))
                    .frame(width: 6, height: 6)
            }
        }
        .accessibilityLabel("Question \(currentPage + 1) of \(pageCount)")
    }

    @ViewBuilder
    private var footer: some View {
        HStack(spacing: 8) {
            if pauseRequest.canRespond {
                if pageCount > 1 && currentPage > 0 {
                    Button {
                        withAnimation { currentPage -= 1 }
                    } label: {
                        Label("Back", systemImage: "chevron.left")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(isDisabled)
                    .accessibilityIdentifier("session-pause-back")
                }

                if pageCount > 1 && !isLastPage {
                    Button {
                        withAnimation { currentPage += 1 }
                    } label: {
                        Label("Next", systemImage: "chevron.right")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(isDisabled || !currentPageAnswered)
                    .accessibilityHint(currentPageAnswered ? "" : "Select an option to continue")
                    .accessibilityIdentifier("session-pause-next")
                } else {
                    Button {
                        Task { await submitAnswer() }
                    } label: {
                        if isResponding {
                            ProgressView().controlSize(.mini)
                        } else {
                            Label(primaryActionLabel, systemImage: "checkmark.circle")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(!canSubmitAnswer || isDisabled)
                    .accessibilityIdentifier("session-pause-send")
                }

                Button {
                    Task { await cancelRequest() }
                } label: {
                    Label(secondaryActionLabel, systemImage: "xmark.circle")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(isDisabled)
                .accessibilityIdentifier("session-pause-cancel")
            } else {
                Label("Waiting in terminal", systemImage: "terminal")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private var currentPageAnswered: Bool {
        guard pauseRequest.questions.indices.contains(currentPage) else { return true }
        let question = pauseRequest.questions[currentPage]
        let key = Self.questionKey(question, index: currentPage)
        return answers[key, default: []].contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    @ViewBuilder
    private func questionView(question: SessionPauseQuestion, index: Int) -> some View {
        let key = Self.questionKey(question, index: index)
        VStack(alignment: .leading, spacing: 5) {
            if let header = question.header?.nonEmptyTrimmed {
                Text(header)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            Text(question.question)
                .font(.caption)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)

            if isPlanApproval {
                EmptyView()
            } else if question.options.isEmpty {
                if pauseRequest.canRespond {
                    TextField("Answer", text: Binding(
                        get: { answers[key]?.first ?? "" },
                        set: { answers[key] = [$0] }
                    ), axis: .vertical)
                    .lineLimit(1...3)
                    .disabled(isDisabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(question.options.enumerated()), id: \.offset) { optionIndex, option in
                        optionButton(question: question, option: option, key: key, optionIndex: optionIndex)
                    }
                }
            }
        }
    }

    private func optionButton(
        question: SessionPauseQuestion,
        option: SessionPauseQuestionOption,
        key: String,
        optionIndex: Int
    ) -> some View {
        let value = Self.optionValue(option)
        let selected = answers[key, default: []].contains(value)
        return Button {
            if question.multiSelect {
                toggleValue(value, for: key)
            } else {
                answers[key] = [value]
            }
        } label: {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: selected ? "checkmark.circle.fill" : (question.multiSelect ? "square" : "circle"))
                    .font(.caption)
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .frame(width: 16, height: 16)
                VStack(alignment: .leading, spacing: 1) {
                    Text(option.label)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let description = option.description?.nonEmptyTrimmed {
                        Text(description)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isDisabled)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("session-pause-option-\(key)-\(optionIndex)")
    }

    private var providerLabel: String {
        pauseRequest.provider.prefix(1).uppercased() + String(pauseRequest.provider.dropFirst())
    }

    private var detailText: String {
        if let summary = pauseRequest.summary?.nonEmptyTrimmed {
            return summary
        }
        return pauseRequest.canRespond
            ? "\(providerLabel) is waiting for your answer."
            : "Answer this in the terminal or reconnect the host."
    }

    private var isDisabled: Bool {
        isResponding || submitted || !pauseRequest.canRespond
    }

    // Permission prompts (tool allow/deny) reuse this card but read as Allow/Deny
    // and need no answer text.
    private var isPermissionPrompt: Bool {
        pauseRequest.kind == "permission_prompt"
    }

    private var isPlanApproval: Bool {
        pauseRequest.kind == "plan_approval"
    }

    private var primaryActionLabel: String {
        if isPermissionPrompt { return "Allow" }
        if isPlanApproval { return "Approve" }
        return "Send answer"
    }

    private var secondaryActionLabel: String {
        if isPermissionPrompt { return "Deny" }
        if isPlanApproval { return "Reject" }
        return "Cancel"
    }

    private var canSubmitAnswer: Bool {
        if isPermissionPrompt || isPlanApproval {
            return true
        }
        if pauseRequest.questions.isEmpty {
            return !fallbackText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return pauseRequest.questions.enumerated().allSatisfy { index, question in
            let key = Self.questionKey(question, index: index)
            return answers[key, default: []].contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
    }

    private func submitAnswer() async {
        let structuredAnswers: [String: [String]]?
        let content: String?
        if pauseRequest.questions.isEmpty {
            structuredAnswers = nil
            content = fallbackText.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            structuredAnswers = normalizedAnswers()
            content = nil
        }
        let ok = await onRespond(
            "answer",
            structuredAnswers,
            content,
            answerMessage(structuredAnswers: structuredAnswers, content: content)
        )
        if ok { submitted = true }
    }

    private func cancelRequest() async {
        let ok = await onRespond("cancel", nil, nil, "Cancelled in Longhouse.")
        if ok { submitted = true }
    }

    private func normalizedAnswers() -> [String: [String]] {
        Dictionary(uniqueKeysWithValues: pauseRequest.questions.enumerated().map { index, question in
            let key = Self.questionKey(question, index: index)
            let values = answers[key, default: []]
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            return (key, values)
        })
    }

    private func answerMessage(structuredAnswers: [String: [String]]?, content: String?) -> String? {
        if let structuredAnswers {
            let parts = pauseRequest.questions.enumerated().compactMap { index, question -> String? in
                let key = Self.questionKey(question, index: index)
                guard let values = structuredAnswers[key], !values.isEmpty else { return nil }
                let label = question.header?.nonEmptyTrimmed ?? question.question
                return "\(label): \(values.joined(separator: ", "))"
            }
            return parts.isEmpty ? nil : parts.joined(separator: "; ")
        }
        return content?.nonEmptyTrimmed
    }

    private func toggleValue(_ value: String, for key: String) {
        var values = answers[key, default: []]
        if values.contains(value) {
            values.removeAll { $0 == value }
        } else {
            values.append(value)
        }
        answers[key] = values
    }

    private static func initialAnswers(for questions: [SessionPauseQuestion]) -> [String: [String]] {
        Dictionary(uniqueKeysWithValues: questions.enumerated().map { index, question in
            (questionKey(question, index: index), [])
        })
    }

    private static func questionKey(_ question: SessionPauseQuestion, index: Int) -> String {
        let trimmed = question.id.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "question-\(index + 1)" : trimmed
    }

    private static func optionValue(_ option: SessionPauseQuestionOption) -> String {
        let raw = option.value ?? option.label
        return raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private extension String {
    var nonEmptyTrimmed: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
