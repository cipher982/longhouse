import Foundation
import Testing
@testable import Longhouse

struct LonghouseWebNavigationTests {
    private let serverURL = "https://david010.longhouse.ai"

    @Test
    func allowsSameHostTimelineNavigation() throws {
        let url = try #require(URL(string: "https://david010.longhouse.ai/timeline"))

        #expect(LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .allow)
    }

    @Test
    func interceptsTenantLoginForNativeAuth() throws {
        let url = try #require(URL(string: "https://david010.longhouse.ai/login?return_to=%2Ftimeline%2Fabc"))

        #expect(LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .nativeAuth)
        #expect(
            LonghouseWebNavigation.postLoginPath(from: url, serverURL: serverURL) == "/timeline/abc"
        )
    }

    @Test
    func interceptsHostedControlPlaneAuthForNativeAuth() throws {
        let url = try #require(URL(string: "https://control.longhouse.ai/?return_to=%2Fauth%2Fnative%2Fopen-instance"))

        #expect(LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .nativeAuth)
        #expect(
            LonghouseWebNavigation.postLoginPath(from: url, serverURL: serverURL) ==
                LonghouseWebNavigation.defaultPostLoginPath
        )
    }

    @Test
    func preservesTenantReturnToFromHostedOpenInstance() throws {
        let url = try #require(URL(
            string: "https://control.longhouse.ai/dashboard/open-instance?return_to=%2Ftimeline%2Fabc%3Fview%3Dcompact"
        ))

        #expect(LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .nativeAuth)
        #expect(
            LonghouseWebNavigation.postLoginPath(from: url, serverURL: serverURL) ==
                "/timeline/abc?view=compact"
        )
    }

    @Test
    func rejectsAuthPathAsPostLoginDestination() throws {
        let url = try #require(URL(string: "https://david010.longhouse.ai/login?return_to=%2Fauth%2Frefresh"))

        #expect(
            LonghouseWebNavigation.postLoginPath(from: url, serverURL: serverURL) ==
                LonghouseWebNavigation.defaultPostLoginPath
        )
    }

    @Test
    func routesOtherCrossOriginNavigationToExternalBrowser() throws {
        let url = try #require(URL(string: "https://example.com/docs"))

        #expect(LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .externalBrowser)
    }

    @Test
    func leavesNonAuthControlPlanePagesExternal() throws {
        let url = try #require(URL(string: "https://control.longhouse.ai/dashboard"))

        #expect(LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .externalBrowser)
    }
}
