import Foundation

/// Lite transcript pages (`detail=lite`, server `zerg/services/transcript_lite.py`)
/// carry every event and all conversation text, but send each tool body as the
/// preview its collapsed row shows, each tool presentation once per page, and
/// omit fields at their default value. Hydration rebuilds the full wire shape
/// at the decode boundary, so the generated DTOs and the rest of the app never
/// see the lite form. Mirrors web `shared/api/liteTranscript.ts`.
///
/// A cut event keeps its `cursor` and `tool_input_truncated` /
/// `tool_output_truncated` flags; an expanded row fetches the full body from
/// `/event-bodies` with that cursor.
enum LiteTranscript {
    /// Returns a full-shape mobile-tail body. A full page (an older server, or
    /// `detail=full`) is returned untouched.
    static func hydrateMobileTail(_ data: Data) throws -> Data {
        // Cheap guard: a full page never carries the lite marker, and parsing
        // a large full page twice would cost more than the lite page saves.
        guard data.range(of: Data(#""lite""#.utf8)) != nil,
              var root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let projection = root["projection"] as? [String: Any],
              projection["detail"] as? String == "lite"
        else { return data }
        root["projection"] = hydrateProjection(projection)
        return try JSONSerialization.data(withJSONObject: root)
    }

    static func hydrateProjection(_ wire: [String: Any]) -> [String: Any] {
        var projection = wire
        let presentations = wire["tool_presentations"] as? [String: [String: Any]] ?? [:]
        let focusSessionId = wire["focus_session_id"]
        projection.removeValue(forKey: "detail")
        projection.removeValue(forKey: "tool_presentations")
        let items = (wire["items"] as? [[String: Any]]) ?? []
        projection["items"] = items.map { wireItem -> [String: Any] in
            var item = wireItem
            if item["session_id"] == nil, let focusSessionId {
                item["session_id"] = focusSessionId
            }
            if let event = wireItem["event"] as? [String: Any] {
                item["event"] = hydrateEvent(event, itemTimestamp: wireItem["timestamp"], presentations: presentations)
            }
            return item
        }
        return projection
    }

    private static func hydrateEvent(
        _ wire: [String: Any],
        itemTimestamp: Any?,
        presentations: [String: [String: Any]]
    ) -> [String: Any] {
        var event = wire
        if event["timestamp"] == nil, let itemTimestamp {
            event["timestamp"] = itemTimestamp
        }
        let ref = event.removeValue(forKey: "tool_presentation_ref") as? String
        let presentedInput = event.removeValue(forKey: "tool_presentation_input")
        let shellSummary = event.removeValue(forKey: "tool_presentation_shell_summary")
        let children = event.removeValue(forKey: "tool_presentation_children")
        if let ref, var presentation = presentations[ref] {
            if let same = presentedInput as? String, same == "same" {
                presentation["tool_input_json"] = event["tool_input_json"] ?? NSNull()
            } else if let wrapped = presentedInput as? [String: Any] {
                presentation["tool_input_json"] = wrapped["value"] ?? NSNull()
            }
            if let shellSummary {
                presentation["shell_summary"] = shellSummary
            }
            presentation["children"] = children ?? []
            event["tool_presentation"] = presentation
        }
        return event
    }
}
