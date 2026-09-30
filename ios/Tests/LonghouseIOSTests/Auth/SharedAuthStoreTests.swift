import Foundation
import Testing
@testable import Longhouse

struct SharedAuthStoreTests {
    @Test
    func insecureHTTPOptInCoversOnlyTheAddressItWasGivenFor() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let previousServer = SharedAuthStore.loadServerURL()
        defer {
            SharedAuthStore.saveInsecureHTTPOptIn(for: nil)
            if let previousServer { SharedAuthStore.saveServerURL(previousServer) } else { SharedAuthStore.clearServerURL() }
        }

        SharedAuthStore.saveInsecureHTTPOptIn(for: "http://192.168.1.20:8080/")
        #expect(SharedAuthStore.hasInsecureHTTPOptIn(for: "http://192.168.1.20:8080"))
        #expect(SharedAuthStore.hasInsecureHTTPOptIn(for: "HTTP://192.168.1.20:8080/"))
        #expect(!SharedAuthStore.hasInsecureHTTPOptIn(for: "http://192.168.1.99:8080"))
        #expect(SharedAuthStore.isSameServerAddress(" http://192.168.1.20:8080/ ", "HTTP://192.168.1.20:8080"))
        #expect(!SharedAuthStore.isSameServerAddress("http://192.168.1.20:8080", "http://192.168.1.20:9090"))

        // Saving the same server keeps it; saving a different one drops it.
        SharedAuthStore.saveServerURL("http://192.168.1.20:8080")
        #expect(SharedAuthStore.hasInsecureHTTPOptIn(for: "http://192.168.1.20:8080"))
        SharedAuthStore.saveServerURL("http://192.168.1.99:8080")
        #expect(!SharedAuthStore.hasInsecureHTTPOptIn(for: "http://192.168.1.20:8080"))
        #expect(!SharedAuthStore.hasInsecureHTTPOptIn(for: "http://192.168.1.99:8080"))

        SharedAuthStore.saveInsecureHTTPOptIn(for: "http://192.168.1.99:8080")
        SharedAuthStore.clearServerURL()
        #expect(!SharedAuthStore.hasInsecureHTTPOptIn(for: "http://192.168.1.99:8080"))
    }

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

    @Test
    func staleHostedTokenGenerationCannotRestoreCredentials() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://generation-test.longhouse.ai"
        SharedAuthStore.clearRuntimeToken(for: serverURL)
        let oldGeneration = SharedAuthStore.authGeneration(for: serverURL)
        let currentGeneration = SharedAuthStore.advanceAuthGeneration(for: serverURL)

        #expect(!SharedAuthStore.saveHostedTokens(
            runtimeToken: "stale-runtime",
            runtimeExpiresAt: nil,
            refreshToken: "stale-refresh",
            refreshExpiresAt: nil,
            for: serverURL,
            expectedGeneration: oldGeneration
        ))
        #expect(SharedAuthStore.runtimeToken(for: serverURL) == nil)
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == nil)

        #expect(SharedAuthStore.saveHostedTokens(
            runtimeToken: "current-runtime",
            runtimeExpiresAt: nil,
            refreshToken: "current-refresh",
            refreshExpiresAt: nil,
            for: serverURL,
            expectedGeneration: currentGeneration
        ))
        #expect(SharedAuthStore.runtimeToken(for: serverURL) == "current-runtime")
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "current-refresh")
        SharedAuthStore.clearRuntimeToken(for: serverURL)
    }

    @Test
    func pendingNativeRevocationsKeepFamiliesIndependent() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://pending-revocations-test.longhouse.ai"
        SharedAuthStore.clearPendingNativeRevocationToken(for: serverURL)

        SharedAuthStore.savePendingNativeRevocationToken("family-a", for: serverURL)
        SharedAuthStore.savePendingNativeRevocationToken("family-b", for: serverURL)
        SharedAuthStore.savePendingNativeRevocationToken("family-a", for: serverURL)

        #expect(SharedAuthStore.pendingNativeRevocationTokens(for: serverURL) == ["family-a", "family-b"])
        SharedAuthStore.clearPendingNativeRevocationToken("family-a", for: serverURL)
        #expect(SharedAuthStore.pendingNativeRevocationTokens(for: serverURL) == ["family-b"])

        SharedAuthStore.clearPendingNativeRevocationToken("family-b", for: serverURL)
        #expect(SharedAuthStore.pendingNativeRevocationTokens(for: serverURL).isEmpty)
    }

    @Test
    func openAccessIsPerHostAndOnlyWhenSet() throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let open = "https://open-access-test.longhouse.ai"
        let other = "https://other-access-test.longhouse.ai"
        SharedAuthStore.setOpenAccess(false, for: open)
        #expect(!SharedAuthStore.hasOpenAccess(for: open))

        SharedAuthStore.setOpenAccess(true, for: open)
        #expect(SharedAuthStore.hasOpenAccess(for: open))
        #expect(!SharedAuthStore.hasOpenAccess(for: other))

        SharedAuthStore.setOpenAccess(false, for: open)
        #expect(!SharedAuthStore.hasOpenAccess(for: open))
    }
}
