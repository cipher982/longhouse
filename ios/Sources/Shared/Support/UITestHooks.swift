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
    static let inboxGalleryFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_INBOX_GALLERY_FIXTURE"
    static let hearthLiveEnvironmentKey = "LONGHOUSE_UI_TEST_HEARTH_LIVE"
    static let hearthPitchEnvironmentKey = "LONGHOUSE_UI_TEST_HEARTH_PITCH"
    static let launchSessionFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_LAUNCH_SESSION_FIXTURE"
    static let machinesFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_MACHINES_FIXTURE"
    static let machineFixtureScreenEnvironmentKey = "LONGHOUSE_UI_TEST_MACHINE_SCREEN"
    static let mobileDetailDelayMsEnvironmentKey = "LONGHOUSE_UI_TEST_MOBILE_DETAIL_DELAY_MS"
    static let mobileTailDelayMsEnvironmentKey = "LONGHOUSE_UI_TEST_MOBILE_TAIL_DELAY_MS"
    static let transcriptBenchmarkRendererEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_RENDERER"
    static let transcriptBenchmarkAutoStartEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_AUTO_START"
    static let transcriptBenchmarkRunIDEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_RUN_ID"
    static let transcriptBenchmarkBuildConfigurationEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_BUILD_CONFIGURATION"
    static let transcriptBenchmarkDebuggerEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_DEBUGGER"
    static let transcriptBenchmarkTemperatureEnvironmentKey = "LONGHOUSE_TRANSCRIPT_BENCHMARK_TEMPERATURE"
    static let appearanceOverrideArgument = "-LONGHOUSE_UI_TEST_APPEARANCE"
    /// A directory a UI test writes fixture commands to (UITestFixtureChannel), so it can
    /// switch the running app to its next fixture instead of relaunching it.
    static let fixtureChannelEnvironmentKey = "LONGHOUSE_UI_TEST_FIXTURE_CHANNEL"

    /// Read once: `ProcessInfo.environment` builds a new dictionary on every access, and
    /// view bodies read these hooks on each render.
    private static let launchEnvironment = ProcessInfo.processInfo.environment

    /// A launch-time value, or the value the last fixture command set. A command replaces
    /// every LONGHOUSE_UI_TEST_ key at once, as a relaunch with its environment would.
    private static func environmentValue(_ key: String) -> String? {
        if key != fixtureChannelEnvironmentKey,
           key.hasPrefix("LONGHOUSE_UI_TEST_"),
           let replaced = UITestFixtureOverrides.shared.environment {
            return replaced[key]
        }
        return launchEnvironment[key]
    }

    static var fixtureChannelDirectory: String? {
        nonemptyEnvironmentValue(fixtureChannelEnvironmentKey)
    }

    static var shouldResetState: Bool {
        environmentValue(resetStateEnvironmentKey) == "1"
    }

    static var shouldCaptureHostedAuthAttempt: Bool {
        environmentValue(captureHostedAuthEnvironmentKey) == "1"
    }

    static var chatFixtureName: String? {
        let raw = environmentValue(chatFixtureEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureEventCount: Int? {
        guard let raw = environmentValue(chatFixtureEventCountEnvironmentKey) else {
            return nil
        }
        return Int(raw)
    }

    static var chatFixtureProbePath: String? {
        let raw = environmentValue(chatFixtureProbePathEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureChurnTriggerPath: String? {
        let raw = environmentValue(chatFixtureChurnTriggerPathEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureTriggerPath: String? {
        let raw = environmentValue(chatFixtureTriggerPathEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var chatFixtureReplayPath: String? {
        let raw = environmentValue(chatFixtureReplayPathEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }

    static var shouldUseTimelineOpenFixture: Bool {
        environmentValue(timelineOpenFixtureEnvironmentKey) == "1"
    }

    /// XCUITest waits for the app to go quiescent before every query, and a
    /// continuously redrawing view never lets it. Fixture-driven runs hold
    /// ambient motion still; the state they assert on does not depend on it.
    static var holdsAmbientMotion: Bool {
        chatFixtureName != nil || shouldUseTimelineOpenFixture || shouldUseLaunchSessionFixture || shouldUseInboxGalleryFixture || shouldUseMachinesFixture
    }

    static var shouldUseInboxGalleryFixture: Bool {
        environmentValue(inboxGalleryFixtureEnvironmentKey) == "1"
    }

    /// The inbox gallery fixture advances its open sessions' counters every
    /// 1.2 s, so the timeline fires flare and spark as on a live card.
    static var shouldRunHearthLive: Bool {
        environmentValue(hearthLiveEnvironmentKey) == "1"
    }

    /// The inbox gallery fixture plays the scripted Hearth pitch scene
    /// (HearthPitchFixture) instead of its static rows.
    static var shouldRunHearthPitch: Bool {
        environmentValue(hearthPitchEnvironmentKey) == "1"
    }

    static var shouldUseLaunchSessionFixture: Bool {
        environmentValue(launchSessionFixtureEnvironmentKey) == "1"
    }
    static var shouldUseMachinesFixture: Bool {
        environmentValue(machinesFixtureEnvironmentKey) == "1"
    }

    static var machineFixtureScreen: String {
        let value = environmentValue(machineFixtureScreenEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return value?.isEmpty == false ? value! : "machines"
    }
    static var mobileDetailDelayMs: Int? {
        guard let raw = environmentValue(mobileDetailDelayMsEnvironmentKey) else {
            return nil
        }
        return Int(raw)
    }

    static var mobileTailDelayMs: Int? {
        guard let raw = environmentValue(mobileTailDelayMsEnvironmentKey) else {
            return nil
        }
        return Int(raw)
    }

    static var transcriptBenchmarkRenderer: String? {
        let raw = environmentValue(transcriptBenchmarkRendererEnvironmentKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return raw?.isEmpty == false ? raw : nil
    }

    static var shouldAutoStartTranscriptBenchmark: Bool {
        environmentValue(transcriptBenchmarkAutoStartEnvironmentKey) == "1"
    }

    static var transcriptBenchmarkRunID: String? {
        let raw = environmentValue(transcriptBenchmarkRunIDEnvironmentKey)?
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
        let override = UITestFixtureOverrides.shared.appearance
        guard let raw = (override ?? launchArgumentValue(for: appearanceOverrideArgument))?
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
        let raw = environmentValue(key)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw?.isEmpty == false ? raw : nil
    }
}

/// What the last UI-test fixture command replaced (see UITestFixtureChannel). Empty until a
/// test sends one, so a normal launch reads its own environment and arguments.
final class UITestFixtureOverrides: @unchecked Sendable {
    static let shared = UITestFixtureOverrides()

    private let lock = NSLock()
    private var replacedEnvironment: [String: String]?
    private var replacedAppearance: String??

    /// The LONGHOUSE_UI_TEST_ environment the running fixture was given, or nil before any command.
    var environment: [String: String]? {
        lock.withLock { replacedEnvironment }
    }

    /// "light" or "dark" from the last command; nil before any command or when it set none.
    var appearance: String? {
        lock.withLock { replacedAppearance ?? nil }
    }

    func replace(environment: [String: String], appearance: String?) {
        lock.withLock {
            replacedEnvironment = environment
            replacedAppearance = .some(appearance)
        }
    }
}
