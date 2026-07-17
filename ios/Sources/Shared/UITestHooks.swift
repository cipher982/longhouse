import Foundation

enum UITestHooks {
    static let resetStateEnvironmentKey = "LONGHOUSE_UI_TEST_RESET_STATE"
    static let captureHostedAuthEnvironmentKey = "LONGHOUSE_UI_TEST_CAPTURE_HOSTED_AUTH"
    static let chatFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
    static let chatFixtureEventCountEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"
    static let chatFixtureProbePathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_PROBE_PATH"
    static let chatFixtureChurnTriggerPathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_CHURN_TRIGGER_PATH"
    static let chatFixtureTriggerPathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_TRIGGER_PATH"
    static let chatFixtureReplayPathEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH"
    static let timelineOpenFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_TIMELINE_OPEN_FIXTURE"
    static let launchSessionFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_LAUNCH_SESSION_FIXTURE"
    static let mobileTailDelayMsEnvironmentKey = "LONGHOUSE_UI_TEST_MOBILE_TAIL_DELAY_MS"
    static let transcriptBenchmarkRendererEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_RENDERER"
    static let transcriptBenchmarkAutoStartEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_AUTO_START"
    static let transcriptBenchmarkRunIDEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_RUN_ID"
    static let transcriptBenchmarkBuildConfigurationEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_BUILD_CONFIGURATION"
    static let transcriptBenchmarkDebuggerEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_DEBUGGER"
    static let transcriptBenchmarkTemperatureEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_TEMPERATURE"
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

    static var chatFixtureChurnTriggerPath: String? {
        let raw = ProcessInfo.processInfo.environment[chatFixtureChurnTriggerPathEnvironmentKey]?
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

    static var shouldUseLaunchSessionFixture: Bool {
        ProcessInfo.processInfo.environment[launchSessionFixtureEnvironmentKey] == "1"
    }

    static var mobileTailDelayMs: Int? {
        guard let raw = ProcessInfo.processInfo.environment[mobileTailDelayMsEnvironmentKey] else {
            return nil
        }
        return Int(raw)
    }

    static var transcriptBenchmarkRenderer: String? {
        let raw = ProcessInfo.processInfo.environment[transcriptBenchmarkRendererEnvironmentKey]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return raw?.isEmpty == false ? raw : nil
    }

    static var shouldAutoStartTranscriptBenchmark: Bool {
        ProcessInfo.processInfo.environment[transcriptBenchmarkAutoStartEnvironmentKey] == "1"
    }

    static var transcriptBenchmarkRunID: String? {
        let raw = ProcessInfo.processInfo.environment[transcriptBenchmarkRunIDEnvironmentKey]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var transcriptBenchmarkBuildConfiguration: String? {
        nonemptyEnvironmentValue(transcriptBenchmarkBuildConfigurationEnvironmentKey)
    }

    static var transcriptBenchmarkDebugger: String? {
        nonemptyEnvironmentValue(transcriptBenchmarkDebuggerEnvironmentKey)
    }

    static var transcriptBenchmarkTemperature: String? {
        nonemptyEnvironmentValue(transcriptBenchmarkTemperatureEnvironmentKey)
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

    private static func nonemptyEnvironmentValue(_ key: String) -> String? {
        let raw = ProcessInfo.processInfo.environment[key]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }
}
