import Foundation
import Testing
@testable import Longhouse

@Suite(.serialized)
@MainActor
struct DemoAccessTests {
    @Test
    func openAccessServerRestoresWithoutCredentials() async {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://open-restore-test.longhouse.ai"
        SharedAuthStore.saveServerURL(serverURL)
        SharedAuthStore.setOpenAccess(true, for: serverURL)
        defer {
            SharedAuthStore.setOpenAccess(false, for: serverURL)
            SharedAuthStore.clearServerURL()
        }

        let state = AppState()
        await state.restoreSession()

        #expect(state.isAuthenticated)
        #expect(state.isExploringDemo)
    }

    @Test
    func serverWithoutOpenAccessOrCredentialsStaysSignedOut() async {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://closed-restore-test.longhouse.ai"
        SharedAuthStore.saveServerURL(serverURL)
        SharedAuthStore.setOpenAccess(false, for: serverURL)
        defer { SharedAuthStore.clearServerURL() }

        let state = AppState()
        await state.restoreSession()

        #expect(!state.isAuthenticated)
        #expect(!state.isExploringDemo)
    }
}
