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

    @Test
    func nativeRefreshTokenRoundTripsWithExpiry() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://native-refresh-test.longhouse.ai"
        let expiresAt = Date(timeIntervalSince1970: 1_810_000_000)

        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == nil)
        #expect(SharedAuthStore.nativeRefreshTokenExpiresAt(for: serverURL) == nil)

        SharedAuthStore.saveNativeRefreshToken(" refresh-token ", expiresAt: expiresAt, for: serverURL)

        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "refresh-token")
        #expect(SharedAuthStore.hasNativeRefreshToken(for: serverURL))
        let roundTripped = SharedAuthStore.nativeRefreshTokenExpiresAt(for: serverURL)
        #expect(roundTripped != nil)
        if let roundTripped {
            #expect(abs(roundTripped.timeIntervalSince1970 - expiresAt.timeIntervalSince1970) < 1.0)
        }

        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == nil)
        #expect(SharedAuthStore.nativeRefreshTokenExpiresAt(for: serverURL) == nil)
    }

    @Test
    func saveHostedTokensPersistsRefreshBeforeRuntimeAndDebugStateSeesBoth() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://hosted-token-test.longhouse.ai"
        let runtimeExpiresAt = Date(timeIntervalSince1970: 1_820_000_000)
        let refreshExpiresAt = Date(timeIntervalSince1970: 1_830_000_000)

        SharedAuthStore.clearRuntimeToken(for: serverURL)
        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
        SharedAuthStore.clearManagedCookies(for: serverURL)

        SharedAuthStore.saveHostedTokens(
            runtimeToken: "runtime-token",
            runtimeExpiresAt: runtimeExpiresAt,
            refreshToken: "refresh-token",
            refreshExpiresAt: refreshExpiresAt,
            for: serverURL
        )

        #expect(SharedAuthStore.runtimeToken(for: serverURL) == "runtime-token")
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "refresh-token")
        let state = SharedAuthStore.debugState(for: serverURL)
        #expect(state.hasRuntimeToken)
        #expect(state.hasNativeRefreshToken)
        #expect(state.hasCredentials)

        SharedAuthStore.clearRuntimeToken(for: serverURL)
        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
    }
}
