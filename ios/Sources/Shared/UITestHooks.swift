import Foundation

enum UITestHooks {
    static let resetStateEnvironmentKey = "LONGHOUSE_UI_TEST_RESET_STATE"
    static let captureHostedAuthEnvironmentKey = "LONGHOUSE_UI_TEST_CAPTURE_HOSTED_AUTH"

    static var shouldResetState: Bool {
        ProcessInfo.processInfo.environment[resetStateEnvironmentKey] == "1"
    }

    static var shouldCaptureHostedAuthAttempt: Bool {
        ProcessInfo.processInfo.environment[captureHostedAuthEnvironmentKey] == "1"
    }
}
