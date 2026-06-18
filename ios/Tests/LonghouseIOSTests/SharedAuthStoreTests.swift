import Foundation
import Testing
@testable import Longhouse

struct SharedAuthStoreTests {
    @Test
    func runtimeTokenExpiryRoundTripsThroughDefaults() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://expiry-test.longhouse.ai"
        SharedAuthStore.clearRuntimeToken(for: serverURL)
        #expect(SharedAuthStore.runtimeTokenExpiresAt(for: serverURL) == nil)

        let expiresAt = Date(timeIntervalSince1970: 1_800_000_000)
        SharedAuthStore.saveRuntimeToken("test-token", expiresAt: expiresAt, for: serverURL)

        let roundTripped = SharedAuthStore.runtimeTokenExpiresAt(for: serverURL)
        #expect(roundTripped != nil)
        if let roundTripped {
            #expect(abs(roundTripped.timeIntervalSince1970 - expiresAt.timeIntervalSince1970) < 1.0)
        }

        SharedAuthStore.clearRuntimeToken(for: serverURL)
        #expect(SharedAuthStore.runtimeTokenExpiresAt(for: serverURL) == nil)
    }

    @Test
    func saveRuntimeTokenWithoutExpiryStoresNilExpiry() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://no-expiry-test.longhouse.ai"
        SharedAuthStore.clearRuntimeToken(for: serverURL)
        SharedAuthStore.saveRuntimeToken("test-token", for: serverURL)
        #expect(SharedAuthStore.runtimeTokenExpiresAt(for: serverURL) == nil)
        #expect(SharedAuthStore.hasRuntimeToken(for: serverURL))
        SharedAuthStore.clearRuntimeToken(for: serverURL)
    }
}
