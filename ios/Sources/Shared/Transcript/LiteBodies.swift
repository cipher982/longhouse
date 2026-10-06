import Foundation

/// Full tool bodies the session has loaded for rows a lite page sent as
/// previews, plus which ones are in flight or no longer exist. Bodies are
/// immutable for a cursor, so they stay for the life of the session screen.
struct LiteBodyState: Sendable, Equatable {
    var bodies: [String: SessionEventBody] = [:]
    var loading: Set<String> = []
    /// Cursors the server reported missing: the session was re-rendered since
    /// the page loaded. Those rows keep their preview and say so.
    var unavailable: Set<String> = []

    func merged(_ event: SessionEvent) -> SessionEvent {
        guard let cursor = event.liteBodyCursor, let body = bodies[cursor] else { return event }
        return event.withFullBody(body)
    }

    func merged(_ item: TimelineItem) -> TimelineItem {
        guard !bodies.isEmpty else { return item }
        switch item {
        case .tool(let call, let result, let pairing):
            return .tool(call: merged(call), result: result.map(merged), pairing: pairing)
        case .orphanTool(let event):
            return .orphanTool(merged(event))
        case .activityGroup(let calls):
            return .activityGroup(calls: calls.map {
                ActivityCall(call: merged($0.call), result: $0.result.map(merged), pairing: $0.pairing)
            })
        case .user, .assistant, .providerNotification, .action:
            return item
        }
    }

    /// What an expanded row shows below a preview: `loading` while its bodies
    /// are in flight, `unavailable` once the server no longer has them, and
    /// `preview` before anything was asked. Nil when nothing is cut.
    func state(for cursors: [String]) -> String? {
        guard !cursors.isEmpty else { return nil }
        if cursors.contains(where: loading.contains) { return "loading" }
        if cursors.allSatisfy(unavailable.contains) { return "unavailable" }
        return "preview"
    }
}

extension TimelineItem {
    /// Cursors of this row's events whose bodies a lite page cut.
    var liteBodyCursors: [String] {
        switch self {
        case .tool(let call, let result, _):
            return [call.liteBodyCursor, result?.liteBodyCursor].compactMap { $0 }.uniqued()
        case .orphanTool(let event):
            return [event.liteBodyCursor].compactMap { $0 }
        case .activityGroup(let calls):
            return calls.flatMap { [$0.call.liteBodyCursor, $0.result?.liteBodyCursor].compactMap { $0 } }.uniqued()
        case .user, .assistant, .providerNotification, .action:
            return []
        }
    }
}

private extension Array where Element == String {
    func uniqued() -> [String] {
        var seen = Set<String>()
        return filter { seen.insert($0).inserted }
    }
}
