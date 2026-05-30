import Foundation

enum UITestHooks {
    static let resetStateEnvironmentKey = "LONGHOUSE_UI_TEST_RESET_STATE"
    static let captureHostedAuthEnvironmentKey = "LONGHOUSE_UI_TEST_CAPTURE_HOSTED_AUTH"
    static let chatFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
    static let chatFixtureEventCountEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
    static let chatFixtureProbePathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_PROBE_PATH"
    static let chatFixtureTriggerPathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_TRIGGER_PATH"
    static let chatFixtureReplayPathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH"
    static let timelineOpenFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_TIMELINE_OPEN_FIXTURE"
    static let mobileTailDelayMsEnvironmentKey = "LONGHOUSE_UI_TEST_MOBILE_TAIL_DELAY_MS"
    static let appearanceOverrideArgument = "-LONGHOUSE_UI_TEST_APPEARANCE"

    static var shouldResetState: Bool {
        ProcessInfo.processInfo.environment[resetStateEnvironmentKey] == "1"
    }

    static var shouldCaptureHostedAuthAttempt: Bool {
        ProcessInfo.processInfo.environment[captureHostedAuthEnvironmentKey] == "1"
    }

    static var chatFixtureName: String? {
        let raw = ProcessInfo.processInfo.environment[chatFixtureEnvironmentKey]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureEventCount: Int? {
        guard let raw = ProcessInfo.processInfo.environment[chatFixtureEventCountEnvironmentKey] else {
            return nil
        }
        return Int(raw)
    }

    static var chatFixtureProbePath: String? {
        let raw = ProcessInfo.processInfo.environment[chatFixtureProbePathEnvironmentKey]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureTriggerPath: String? {
        let raw = ProcessInfo.processInfo.environment[chatFixtureTriggerPathEnvironmentKey]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureReplayPath: String? {
        let raw = ProcessInfo.processInfo.environment[chatFixtureReplayPathEnvironmentKey]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var shouldUseTimelineOpenFixture: Bool {
        ProcessInfo.processInfo.environment[timelineOpenFixtureEnvironmentKey] == "1"
    }

    static var mobileTailDelayMs: Int? {
        guard let raw = ProcessInfo.processInfo.environment[mobileTailDelayMsEnvironmentKey] else {
            return nil
        }
        return Int(raw)
    }

    static var appearanceOverride: String? {
        guard let raw = launchArgumentValue(for: appearanceOverrideArgument)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() else {
            return nil
        }
        switch raw {
        case "light", "dark":
            return raw
        default:
            return nil
        }
    }

    private static func launchArgumentValue(for name: String) -> String? {
        let arguments = ProcessInfo.processInfo.arguments
        for index in arguments.indices {
            let argument = arguments[index]
            if argument == name, arguments.indices.contains(index + 1) {
                return arguments[index + 1]
            }
            if argument.hasPrefix("\(name)=") {
                return String(argument.dropFirst(name.count + 1))
            }
        }
        return nil
    }
}
