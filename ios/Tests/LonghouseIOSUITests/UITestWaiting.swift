import XCTest

/// Waits that check right away and then every 0.1 s.
///
/// XCTest's own `waitForExistence(timeout:)` and `XCTNSPredicateExpectation`
/// check for the first time about one second after they start, even when the
/// element is already on screen: run 45647963919 spent 1.03 s on every such
/// wait, about 125 s of the 545 s UI scheme. These return the same answer
/// under the same timeout, only sooner.
@MainActor
func poll(timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if condition() { return true }
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    return condition()
}

extension XCUIElement {
    /// `waitForExistence(timeout:)`, polled every 0.1 s from the start.
    @MainActor
    func waitToExist(timeout: TimeInterval) -> Bool {
        poll(timeout: timeout) { exists }
    }
}
