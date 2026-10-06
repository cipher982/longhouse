import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

extension WebTranscriptView {
    struct WebTranscriptPayloadInput: Sendable {
        let serverURL: String?
        let timelineItems: [TimelineItem]
        let subagents: [SessionSubagent]
        let submittedInputs: [SubmittedInput]
        let errorMessage: String?
        let contentRevision: UInt64
        let transcriptReadThrough: String?
        let retryRevision: UInt64
        let sourceRevision: Int?
        let sourceOperation: String?
        var liteBodies = LiteBodyState()
    }

    nonisolated static func preparedPayload(
        serverURL: String? = nil,
        timelineItems: [TimelineItem],
        subagents: [SessionSubagent] = [],
        submittedInputs: [SubmittedInput],
        errorMessage: String?,
        contentRevision: UInt64 = 0,
        transcriptReadThrough: String? = nil,
        retryRevision: UInt64 = 0,
        sourceRevision: Int? = nil,
        sourceOperation: String? = nil,
        liteBodies: LiteBodyState = LiteBodyState()
    ) -> WebTranscriptPreparedPayload {
        let startedAt = Date()
        let payload = WebTranscriptPayload(
            errorMessage: errorMessage,
            items: Self.payloadItems(
                serverURL: serverURL,
                timelineItems: timelineItems,
                subagents: subagents,
                submittedInputs: submittedInputs,
                liteBodies: liteBodies
            )
        )
        let encoder = JSONEncoder()
        let data = (try? encoder.encode(payload)) ?? Data()
        return WebTranscriptPreparedPayload(
            base64: data.base64EncodedString(),
            payloadByteSize: data.count,
            rowCount: payload.items.count,
            latestItemId: payload.items.last?.id,
            payloadFingerprint: Self.payloadFingerprint(data),
            prepareDurationMs: Int(Date().timeIntervalSince(startedAt) * 1000),
            contentRevision: contentRevision,
            transcriptReadThrough: transcriptReadThrough,
            retryRevision: retryRevision,
            sourceRevision: sourceRevision,
            sourceOperation: sourceOperation
        )
    }

    nonisolated static func preparedPayload(
        input: WebTranscriptPayloadInput
    ) -> WebTranscriptPreparedPayload {
        preparedPayload(
            serverURL: input.serverURL,
            timelineItems: input.timelineItems,
            subagents: input.subagents,
            submittedInputs: input.submittedInputs,
            errorMessage: input.errorMessage,
            contentRevision: input.contentRevision,
            transcriptReadThrough: input.transcriptReadThrough,
            retryRevision: input.retryRevision,
            sourceRevision: input.sourceRevision,
            sourceOperation: input.sourceOperation,
            liteBodies: input.liteBodies
        )
    }

    private nonisolated static func payloadFingerprint(_ data: Data) -> String {
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in data {
            hash ^= UInt64(byte)
            hash &*= 0x100000001b3
        }
        return String(format: "%016llx", hash)
    }

    nonisolated static func payloadItems(
        serverURL: String? = nil,
        timelineItems: [TimelineItem],
        subagents: [SessionSubagent] = [],
        submittedInputs: [SubmittedInput],
        liteBodies: LiteBodyState = LiteBodyState()
    ) -> [WebTranscriptPayloadItem] {
        let durableUserInputs = durableUserInputIdentities(timelineItems)
        let visibleSubmittedInputs = submittedInputs.filter { !durableUserInputs.contains($0) }
        let payloadItem: (TimelineItem) -> WebTranscriptPayloadItem = {
            Self.payloadItem($0, serverURL: serverURL, subagents: subagents, liteBodies: liteBodies)
        }
        guard !visibleSubmittedInputs.isEmpty else {
            return timelineItems.map(payloadItem)
        }

        var rows: [WebTranscriptPayloadItem] = []
        var remainingSubmittedInputs = visibleSubmittedInputs
        for item in timelineItems {
            if let previewDate = liveProvisionalAssistantDate(item) {
                let insertion = submittedInputsToPlaceBeforeLivePreview(
                    remainingSubmittedInputs,
                    previewDate: previewDate
                )
                if !insertion.isEmpty {
                    let insertionIds = Set(insertion.map(\.id))
                    rows.append(contentsOf: insertion.map(payloadSubmittedInput))
                    remainingSubmittedInputs.removeAll { insertionIds.contains($0.id) }
                }
            }
            rows.append(payloadItem(item))
        }

        rows.append(contentsOf: remainingSubmittedInputs.map(payloadSubmittedInput))
        return rows
    }

    private nonisolated static func liveProvisionalAssistantDate(_ item: TimelineItem) -> Date? {
        guard case .assistant(let event) = item else { return nil }
        guard event.eventOrigin == "live_provisional" || event.isSynthetic else { return nil }
        return LonghouseDateParser.parse(event.timestamp) ?? .distantPast
    }

    private nonisolated static func submittedInputsToPlaceBeforeLivePreview(
        _ inputs: [SubmittedInput],
        previewDate: Date
    ) -> [SubmittedInput] {
        inputs.filter { input in
            switch input.phase {
            case .submitting, .working, .sent:
                return true
            case .queued:
                return input.createdAt <= previewDate
            case .couldNotConfirm, .failed, .needsUserDecision:
                return false
            }
        }
    }

    private nonisolated static func durableUserInputIdentities(_ timelineItems: [TimelineItem]) -> DurableUserInputIdentities {
        var sessionInputIds = Set<Int>()
        var clientRequestIds = Set<String>()

        for item in timelineItems {
            guard case .user(let event) = item,
                  event.isHeadBranch,
                  let origin = event.inputOrigin else { continue }
            if let sessionInputId = origin.sessionInputId {
                sessionInputIds.insert(sessionInputId)
            }
            if let clientRequestId = origin.clientRequestId,
               !clientRequestId.isEmpty {
                clientRequestIds.insert(clientRequestId)
            }
        }

        return DurableUserInputIdentities(
            sessionInputIds: sessionInputIds,
            clientRequestIds: clientRequestIds
        )
    }

    private nonisolated static func payloadItem(
        _ item: TimelineItem,
        serverURL: String?,
        subagents: [SessionSubagent] = [],
        liteBodies: LiteBodyState = LiteBodyState()
    ) -> WebTranscriptPayloadItem {
        // Loaded full bodies replace a lite page's previews before anything
        // is derived from them (summary, edit stat, failure, output).
        let item = liteBodies.merged(item)
        var payload = payloadItemBody(item, serverURL: serverURL, subagents: subagents)
        payload.turnEnd = turnEndPayload(for: item)
        let cursors = item.liteBodyCursors
        if !cursors.isEmpty {
            payload.bodyCursors = cursors
            payload.bodyState = liteBodies.state(for: cursors)
        }
        return payload
    }

    /// The provider stamps its turn accounting on one durable event; the
    /// footer belongs under whichever row carries that event.
    nonisolated static func turnEndPayload(for item: TimelineItem, now: Date = Date()) -> WebTranscriptTurnEnd? {
        let turnEnd: SessionTurnEnd?
        switch item {
        case .user(let event), .assistant(let event), .orphanTool(let event):
            turnEnd = event.turnEnd
        case .providerNotification:
            turnEnd = nil
        case .tool(let call, let result, _):
            turnEnd = result?.turnEnd ?? call.turnEnd
        case .activityGroup(let calls):
            turnEnd = calls.reversed().lazy.compactMap { $0.result?.turnEnd ?? $0.call.turnEnd }.first
        case .action:
            turnEnd = nil
        }
        guard let turnEnd else { return nil }
        let stopped = turnEnd.outcome == "aborted"
        return WebTranscriptTurnEnd(
            label: "\(stopped ? "Interrupted after" : "Worked for") \(TurnEndCopy.duration(milliseconds: turnEnd.durationMs))",
            doneAt: TurnEndCopy.doneAt(turnEnd.endedAt, now: now, verb: stopped ? "stopped" : "Turn finished")
        )
    }

    private nonisolated static func payloadItemBody(
        _ item: TimelineItem,
        serverURL: String?,
        subagents: [SessionSubagent] = []
    ) -> WebTranscriptPayloadItem {
        switch item {
        case .user(let event):
            if event.inputOrigin?.origin == "wake" {
                return providerNotificationPayload(
                    id: item.id,
                    text: event.contentText ?? "Background task finished"
                )
            }
            return messagePayload(
                id: item.id,
                role: "user",
                text: event.contentText ?? "",
                origin: event.inputOrigin?.authoredVia,
                mediaRefs: payloadMediaRefs(event.mediaRefs, serverURL: serverURL)
            )
        case .assistant(let event):
            return messagePayload(
                id: item.id,
                role: "assistant",
                text: event.contentText ?? "",
                origin: nil,
                mediaRefs: payloadMediaRefs(event.mediaRefs, serverURL: serverURL)
            )
        case .providerNotification(let event):
            return providerNotificationPayload(id: item.id, text: event.contentText ?? "")
        case .action(let action, _):
            return actionPayload(id: item.id, action: action)
        case .tool(let call, let result, _):
            return toolPayload(id: item.id, call: call, result: result, serverURL: serverURL, subagents: subagents)
        case .orphanTool(let event):
            return toolPayload(
                id: item.id,
                call: event,
                result: event,
                orphan: true,
                serverURL: serverURL,
                subagents: subagents
            )
        case .activityGroup(let calls):
            return activityGroupPayload(
                id: item.id,
                calls: calls,
                serverURL: serverURL,
                subagents: subagents
            )
        }
    }

    private nonisolated static func messagePayload(
        id: String,
        role: String,
        text: String,
        origin: SessionInputAuthoredVia?,
        mediaRefs: [WebTranscriptMediaRef]? = nil
    ) -> WebTranscriptPayloadItem {
        let displayText = text
        let collapsed = TranscriptTextPolicy.shouldCollapseMessage(displayText)
        return WebTranscriptPayloadItem(
            id: id,
            kind: "message",
            role: role,
            title: nil,
            subtitle: nil,
            body: TranscriptTextPolicy.visibleMessage(displayText, expanded: false),
            fullBody: collapsed ? displayText : nil,
            collapsed: collapsed,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: origin?.payloadValue,
            media: mediaRefs
        )
    }

    private nonisolated static func actionPayload(id: String, action: SessionAction) -> WebTranscriptPayloadItem {
        WebTranscriptPayloadItem(
            id: id,
            kind: "action",
            role: nil,
            title: actionLabel(action.kind),
            subtitle: action.provider,
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: action.kind,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: nil,
            media: nil
        )
    }

    private nonisolated static func providerNotificationPayload(
        id: String,
        text: String
    ) -> WebTranscriptPayloadItem {
        WebTranscriptPayloadItem(
            id: id,
            kind: "providerNotification",
            role: nil,
            title: nil,
            subtitle: nil,
            body: text,
            fullBody: nil,
            collapsed: false,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: nil,
            media: nil
        )
    }

    private nonisolated static func actionLabel(_ kind: String) -> String {
        if kind == "turn_interrupted" { return "User interrupted the turn" }
        return "Session action"
    }

    private nonisolated static func payloadSubmittedInput(_ input: SubmittedInput) -> WebTranscriptPayloadItem {
        if input.origin == "wake" {
            return providerNotificationPayload(id: input.id, text: input.text)
        }
        return WebTranscriptPayloadItem(
            id: input.id,
            kind: "submitted",
            role: "user",
            title: nil,
            subtitle: submittedStatus(input.phase, lastError: input.lastError),
            body: input.text,
            fullBody: nil,
            collapsed: false,
            status: input.phase.rawValue,
            duration: nil,
            input: nil,
            output: nil,
            calls: [],
            origin: nil,
            media: nil,
            attachments: input.attachmentSummaries
        )
    }

    private struct TranscriptQuestion: Sendable {
        let id: String
        let header: String?
        let question: String
        let options: [TranscriptQuestionOption]
    }

    private struct TranscriptQuestionOption: Sendable {
        let label: String
        let description: String?
    }

    private nonisolated static func transcriptQuestions(from input: [String: JSONValue]?) -> [TranscriptQuestion] {
        guard let input else { return [] }
        let rawQuestions: [JSONValue]
        if case .array(let questions) = input["questions"] {
            rawQuestions = questions
        } else if input["question"] != nil || input["prompt"] != nil {
            rawQuestions = [.object(input)]
        } else {
            rawQuestions = []
        }

        return rawQuestions.enumerated().compactMap { index, raw in
            guard case .object(let item) = raw else { return nil }
            let rawOptions: [JSONValue]
            if case .array(let options) = item["options"] {
                rawOptions = options
            } else if case .array(let choices) = item["choices"] {
                rawOptions = choices
            } else {
                rawOptions = []
            }
            let options = rawOptions.compactMap(transcriptQuestionOption)
            return TranscriptQuestion(
                id: jsonText(item["id"]) ?? jsonText(item["name"]) ?? jsonText(item["key"]) ?? "question-\(index + 1)",
                header: jsonText(item["header"]) ?? jsonText(item["title"]),
                question: jsonText(item["question"]) ?? jsonText(item["prompt"]) ?? jsonText(item["label"]) ?? "Answer required",
                options: options
            )
        }
    }

    private nonisolated static func transcriptQuestionOption(_ raw: JSONValue) -> TranscriptQuestionOption? {
        if case .object(let item) = raw {
            guard let label = jsonText(item["label"]) ?? jsonText(item["value"]) ?? jsonText(item["text"]) else {
                return nil
            }
            return TranscriptQuestionOption(
                label: label,
                description: jsonText(item["description"]) ?? jsonText(item["detail"])
            )
        }
        guard let label = jsonText(raw) else { return nil }
        return TranscriptQuestionOption(label: label, description: nil)
    }

    private nonisolated static func jsonText(_ value: JSONValue?) -> String? {
        let raw: String?
        switch value {
        case .string(let s): raw = s
        case .int(let n): raw = String(n)
        case .double(let n): raw = String(n)
        case .bool(let b): raw = String(b)
        case .array, .object, .null, .none: raw = nil
        }
        let cleaned = raw?.trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned?.isEmpty == false ? cleaned : nil
    }

    private nonisolated static func toolPayload(
        id: String,
        call: SessionEvent,
        result: SessionEvent?,
        orphan: Bool = false,
        serverURL: String?,
        subagents: [SessionSubagent] = []
    ) -> WebTranscriptPayloadItem {
        let toolName = call.toolName ?? "Tool"
        if toolName == "AskUserQuestion" {
            return askUserQuestionPayload(id: id, call: call, result: result)
        }
        let presentedToolName = TimelineBuilder.presentedToolName(call)
        let resolved = ToolTiers.resolve(presentedToolName)
        let presentation = call.toolPresentation
        let duration = result.flatMap { TimelineBuilder.durationSeconds(call: call, result: $0) }
            .map(TimelineBuilder.formatDuration)
        // iOS previously had no failure surface at all: a non-zero exit or a
        // structured failure rendered as "done". One predicate now drives the
        // chip and the preview, as on web (R4).
        let failed = TimelineBuilder.isFailed(call: call, result: result)
        let exitCode = ShellSalienceClassifier.parseExitCode(result?.toolOutputText)
        let status: String? = {
            if orphan { return "orphan" }
            if call.toolCallState == .running { return "running" }
            if call.toolCallState == .dropped { return "dropped" }
            if failed { return exitCode.map { "exit \($0)" } ?? "failed" }
            if call.toolCallState == .completed { return "done" }
            return nil
        }()
        // Only edits get a stat or a diff. Computing this for every tool would
        // let any input carrying a `text`/`content` key masquerade as a file
        // creation and replace its Input block with a bogus diff.
        let editStat = TimelineBuilder.isEditInteraction(call) ? EditSummary.stat(for: call) : nil
        let editLabel = editStat.flatMap { EditSummary.format($0) }
        var presentationCalls = presentation?.children.map { child in
            WebTranscriptToolCall(
                title: child.label,
                subtitle: child.toolName,
                status: "parsed",
                input: prettyJSONValue(child.toolInputValue),
                rawInput: nil,
                output: presentation?.wrapperRecedes == true ? truncatedOutput(result) : nil,
                media: nil
            )
        } ?? []
        if presentation?.wrapperRecedes == true {
            presentationCalls.append(
                WebTranscriptToolCall(
                    title: "Raw enclosing \(presentation?.sourceToolName ?? toolName)",
                    subtitle: "Provider evidence",
                    status: "raw",
                    input: nil,
                    rawInput: prettyJSONValue(call.toolInputValue),
                    output: nil,
                    media: nil
                )
            )
        }

        let spawned = Subagents.children(
            from: subagents,
            toolCallId: call.toolCallId,
            toolOutputText: result?.toolOutputText
        )

        return WebTranscriptPayloadItem(
            id: id,
            kind: "tool",
            role: nil,
            title: presentation?.label ?? resolved.label,
            subtitle: editLabel ?? TimelineBuilder.inputSummary(for: call),
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: status,
            duration: duration,
            input: prettyJSONValue(
                presentation?.wrapperRecedes == true
                    ? presentation?.toolInputValue
                    : call.toolInputValue
            ) ?? TimelineBuilder.inputSummary(for: call),
            output: truncatedOutput(result),
            calls: presentationCalls,
            origin: presentation?.disposition == "parsed"
                ? "Parsed via \(presentation?.executionMethod ?? presentation?.sourceToolName ?? toolName)"
                : nil,
            media: payloadMediaRefs(call.mediaRefs + (result?.mediaRefs ?? []), serverURL: serverURL),
            failurePreview: TimelineBuilder.failurePreview(call: call, result: result),
            diff: editStat.flatMap { EditSummary.diffLines(for: $0) }.map { lines in
                lines.map { WebTranscriptDiffLine(kind: $0.kind.rawValue, text: $0.text) }
            },
            subagents: spawned.isEmpty
                ? nil
                : spawned.map {
                    WebTranscriptSubagent(
                        sessionId: $0.sessionId,
                        label: Subagents.label(for: $0),
                        toolCalls: $0.toolCalls
                    )
                },
            subagentSummary: spawned.isEmpty ? nil : Subagents.summary(spawned)
        )
    }

    private nonisolated static func askUserQuestionPayload(
        id: String,
        call: SessionEvent,
        result: SessionEvent?
    ) -> WebTranscriptPayloadItem {
        let questions = transcriptQuestions(from: call.toolInputJSON)
        let title = questions.first?.header ?? "Question"
        let body = questions.map(\.question).joined(separator: "\n\n")
        let options = questions.flatMap(\.options).map { option in
            WebTranscriptToolCall(
                title: option.label,
                subtitle: option.description ?? "",
                status: "option",
                input: nil,
                rawInput: nil,
                output: nil,
                media: nil
            )
        }
        return WebTranscriptPayloadItem(
            id: id,
            kind: "question",
            role: nil,
            title: title,
            subtitle: result == nil ? "Answer in terminal" : "Answered in terminal",
            body: body.isEmpty ? "Claude is waiting for your answer." : body,
            fullBody: nil,
            collapsed: false,
            status: result == nil ? "waiting" : "answered",
            duration: nil,
            input: nil,
            output: nil,
            calls: options,
            origin: nil,
            media: nil
        )
    }
    private nonisolated static func activityGroupPayload(
        id: String,
        calls: [ActivityCall],
        serverURL: String?,
        subagents: [SessionSubagent]
    ) -> WebTranscriptPayloadItem {
        let summary = TimelineBuilder.activitySummary(for: calls)
        var seenSubagentIds = Set<String>()
        let spawned = calls
            .flatMap { call in
                Subagents.children(
                    from: subagents,
                    toolCallId: call.call.toolCallId,
                    toolOutputText: call.result?.toolOutputText
                )
            }
            .filter { seenSubagentIds.insert($0.sessionId).inserted }
        let spawnedPayload = spawned.isEmpty
            ? nil
            : spawned.map {
                WebTranscriptSubagent(
                    sessionId: $0.sessionId,
                    label: Subagents.label(for: $0),
                    toolCalls: $0.toolCalls
                )
            }

        // Pass every call; WebKit renderer collapses to latest-N with an
        // interactive "Show N earlier" control (never permanent hide).
        let childCalls = calls.map { passive in
            let status: String = {
                switch passive.call.toolCallState {
                case .running: return "running"
                case .dropped: return "dropped"
                case .completed: return "done"
                case .none: return "done"
                }
            }()
            return WebTranscriptToolCall(
                title: passive.call.toolPresentation?.label
                    ?? ToolTiers.resolve(TimelineBuilder.presentedToolName(passive.call)).label,
                subtitle: TimelineBuilder.inputSummary(for: passive.call),
                status: status,
                input: prettyJSONValue(
                    passive.call.toolPresentation?.wrapperRecedes == true
                        ? passive.call.toolPresentation?.toolInputValue
                        : passive.call.toolInputValue
                ) ?? TimelineBuilder.inputSummary(for: passive.call),
                rawInput: passive.call.toolPresentation?.wrapperRecedes == true
                    ? prettyJSONValue(passive.call.toolInputValue)
                    : nil,
                output: truncatedOutput(passive.result),
                media: payloadMediaRefs(passive.call.mediaRefs + (passive.result?.mediaRefs ?? []), serverURL: serverURL)
            )
        }

        return WebTranscriptPayloadItem(
            id: id,
            kind: "activityGroup",
            role: nil,
            title: summary.isEmpty ? "Activity" : summary,
            subtitle: "\(calls.count)",
            body: nil,
            fullBody: nil,
            collapsed: false,
            status: nil,
            duration: nil,
            input: nil,
            output: nil,
            calls: childCalls,
            origin: nil,
            media: nil,
            subagents: spawnedPayload,
            subagentSummary: spawned.isEmpty ? nil : Subagents.summary(spawned)
        )
    }

    private nonisolated static func payloadMediaRefs(
        _ refs: [SessionEventMediaRef],
        serverURL: String?
    ) -> [WebTranscriptMediaRef]? {
        var seen = Set<String>()
        let media = refs.compactMap { ref -> WebTranscriptMediaRef? in
            let dedupeURL = ref.thumbUrl ?? ref.blobUrl
            let dedupeKey = "\(ref.sha256):\(dedupeURL)"
            guard !seen.contains(dedupeKey) else {
                return nil
            }
            seen.insert(dedupeKey)
            let imageLike = ref.mimeType?.hasPrefix("image/") ?? true
            let visibleURL = ref.mediaState == "present" && imageLike
                ? absoluteMediaURL(ref.thumbUrl ?? ref.blobUrl, serverURL: serverURL)
                : nil
            if visibleURL == nil && ref.mediaState == "present" {
                return nil
            }
            return WebTranscriptMediaRef(
                sha256: ref.sha256,
                url: visibleURL,
                blobUrl: absoluteMediaURL(ref.blobUrl, serverURL: serverURL),
                mediaState: ref.mediaState,
                mimeType: ref.mimeType,
                width: ref.width,
                height: ref.height
            )
        }
        return media.isEmpty ? nil : media
    }

    private nonisolated static func absoluteMediaURL(_ rawURL: String?, serverURL: String?) -> String? {
        guard let rawURL = rawURL?.trimmingCharacters(in: .whitespacesAndNewlines),
              !rawURL.isEmpty else {
            return nil
        }
        if URL(string: rawURL)?.scheme != nil {
            return rawURL
        }
        guard let serverURL,
              let base = URL(string: serverURL),
              let resolved = URL(string: rawURL, relativeTo: base) else {
            return rawURL
        }
        return resolved.absoluteURL.absoluteString
    }

    private nonisolated static func prettyJSON(_ value: [String: JSONValue]?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(value) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private nonisolated static func prettyJSONValue(_ value: JSONValue?) -> String? {
        guard let value else { return nil }
        if case .object(let object) = value {
            return prettyJSON(object)
        }
        if case .string(let string) = value {
            return string
        }
        guard let data = try? JSONEncoder().encode(value),
              let rendered = String(data: data, encoding: .utf8) else { return nil }
        return rendered
    }

    /// A lite preview is already the collapsed row's 2+8 lines, cut one past
    /// 4,096 characters so this marks it; a loaded full body keeps the WebKit
    /// guard of 12,000 characters.
    nonisolated static let previewMaxCharacters = 4_096
    nonisolated static let fullOutputMaxCharacters = 12_000

    private nonisolated static func truncatedOutput(_ result: SessionEvent?) -> String? {
        guard let text = result?.toolOutputText, !text.isEmpty else { return nil }
        let isPreview = result?.toolOutputTruncated == true
        let maxCharacters = isPreview ? previewMaxCharacters : fullOutputMaxCharacters
        guard text.count > maxCharacters else { return text }
        return String(text.prefix(maxCharacters))
            + (isPreview ? "\n… truncated …" : "\n... truncated in iOS transcript ...")
    }

    private nonisolated static func submittedStatus(_ phase: SubmittedInputPhase, lastError: String?) -> String {
        switch phase {
        // One vocabulary with web: "Sending…" only while the POST is in
        // flight. A running Console turn means the server has the input, so
        // it reads "Sent"; turn progress belongs to the activity dock.
        case .submitting: return lastError.map { "Sending… — \($0)" } ?? "Sending…"
        case .working, .sent: return "Sent"
        case .queued: return "Queued · sends after this turn"
        case .couldNotConfirm: return "Not confirmed"
        case .failed: return lastError.map { "Not delivered — \($0)" } ?? "Not delivered"
        case .needsUserDecision:
            return lastError.map { "Needs choice — \($0)" } ?? "Needs choice"
        }
    }
}

struct DurableUserInputIdentities {
    let sessionInputIds: Set<Int>
    let clientRequestIds: Set<String>

    func contains(_ input: SubmittedInput) -> Bool {
        if let serverInputId = input.serverInputId,
           sessionInputIds.contains(serverInputId) {
            return true
        }
        return clientRequestIds.contains(input.clientRequestId)
    }
}

struct WebTranscriptPayload: Encodable {
    let errorMessage: String?
    let items: [WebTranscriptPayloadItem]
}

struct WebTranscriptPayloadItem: Encodable {
    let id: String
    let kind: String
    let role: String?
    let title: String?
    let subtitle: String?
    let body: String?
    let fullBody: String?
    let collapsed: Bool
    let status: String?
    let duration: String?
    let input: String?
    let output: String?
    let calls: [WebTranscriptToolCall]
    let origin: String?
    let media: [WebTranscriptMediaRef]?
    /// Bounded attachment metadata for optimistic submitted rows. Full bytes
    /// never cross the WebView boundary.
    var attachments: [SubmittedInputAttachmentSummary]? = nil
    /// Bounded preview shown without a tap on a failed row (R4).
    var failurePreview: String? = nil
    /// Rendered diff for edit rows (R3).
    var diff: [WebTranscriptDiffLine]? = nil
    /// Workers this call spawned, opened in place rather than spliced in.
    var subagents: [WebTranscriptSubagent]? = nil
    /// "22 agents · 4m12s" — the shape of the work while still collapsed.
    var subagentSummary: String? = nil
    /// "Worked for 2m 9s · Turn finished 9:15 AM" under the item a turn ended on.
    var turnEnd: WebTranscriptTurnEnd? = nil
    /// Cursors of this row's events that a lite page sent as previews.
    /// Expanding the row asks native to load them (`loadToolBodies`).
    var bodyCursors: [String]? = nil
    /// `preview`, `loading`, `failed` or `unavailable` while `bodyCursors` is set.
    var bodyState: String? = nil
}

struct WebTranscriptTurnEnd: Encodable, Equatable {
    let label: String
    let doneAt: String
}

struct WebTranscriptSubagent: Encodable {
    let sessionId: String
    let label: String
    let toolCalls: Int
}

struct WebTranscriptDiffLine: Encodable {
    let kind: String
    let text: String
}

struct WebTranscriptToolCall: Encodable {
    let title: String
    let subtitle: String
    let status: String
    let input: String?
    let rawInput: String?
    let output: String?
    let media: [WebTranscriptMediaRef]?
}

struct WebTranscriptMediaRef: Encodable {
    let sha256: String
    let url: String?
    let blobUrl: String?
    let mediaState: String
    let mimeType: String?
    let width: Int?
    let height: Int?
}

private extension SessionInputAuthoredVia {
    var payloadValue: String {
        switch self {
        case .longhouse:
            return "longhouse"
        case .terminal:
            return "terminal"
        case .unknown(let value):
            return value
        }
    }
}
