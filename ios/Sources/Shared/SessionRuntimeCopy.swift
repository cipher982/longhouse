import Foundation

/// Copy the session chrome derives from served facts. Pure and testable: the
/// header subtitle, the dock's elapsed anchor, and the mono tail line all come
/// from fields the client already holds, so none of this invents state.
extension SessionDetail {
    /// "Codex · zerg · clifford" — provider, project, machine. Identity lives
    /// in the navigation bar; activity lives in the dock.
    var identitySubtitle: String? {
        var parts: [String] = []
        let providerName = ProviderBrands.displayName(provider, fallback: "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !providerName.isEmpty { parts.append(providerName) }
        if let project = project?.trimmingCharacters(in: .whitespacesAndNewlines), !project.isEmpty {
            parts.append(project)
        }
        if let host = identityHost {
            parts.append(host)
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// The registered machine id first. `home_label` is sometimes a machine
    /// name and sometimes a phrase such as "On this Mac"; identity names a
    /// machine or nothing. (`originLabel` is the launch environment, not a host.)
    private static let genericHomeLabels: Set<String> = ["On this Mac", "Hosted", "Moved to cloud", "This machine"]

    var identityHost: String? {
        for candidate in [deviceId, homeLabel] {
            guard let value = candidate?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { continue }
            if Self.genericHomeLabels.contains(value) { continue }
            return value
        }
        return nil
    }

    /// When the current activity observation began. Executing sessions count
    /// up from here; idle ones read it as "last activity".
    var activityStartedAt: Date? {
        let candidates = [stateFacts.activityObservedAt, stateFacts.primary?.observedAt]
        for candidate in candidates {
            if let candidate, let parsed = LonghouseDateParser.parse(candidate) {
                return parsed
            }
        }
        return nil
    }

    /// The newest thing the agent is doing, in its own words: the running
    /// tool's command, or the last line of provisional text. Only while
    /// executing — an idle session has nothing live to say.
    var runtimeTailLine: String? {
        guard isSessionExecuting, let preview = transcriptPreview, preview.isStale != true else { return nil }
        if let toolName = preview.toolName, !toolName.isEmpty {
            if let command = preview.toolInputJSON?["command"], case .string(let value) = command {
                let line = Self.firstMeaningfulLine(value)
                if !line.isEmpty { return "$ " + line }
            }
            if let output = preview.toolOutputText, let line = Self.lastMeaningfulLine(output) {
                return line
            }
            return nil
        }
        guard preview.isProvisional else { return nil }
        return Self.lastMeaningfulLine(preview.text)
    }

    private static func firstMeaningfulLine(_ text: String) -> String {
        text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty } ?? ""
    }

    private static func lastMeaningfulLine(_ text: String) -> String? {
        text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .last { !$0.isEmpty }
    }
}
