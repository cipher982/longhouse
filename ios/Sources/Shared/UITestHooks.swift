import Foundation

enum UITestHooks {
    static let resetStateEnvironmentKey = "LONGHOUSE_UI_TEST_RESET_STATE"
    static let captureHostedAuthEnvironmentKey = "LONGHOUSE_UI_TEST_CAPTURE_HOSTED_AUTH"
    static let chatFixtureEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_FIXTURE"
    static let chatFixtureEventCountEnvironmentKey = "LONGHOUSE_UI_TEST_CHAT_EVENT_COUNT"

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
}
