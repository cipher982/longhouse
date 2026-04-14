import XCTest
@testable import Longhouse

final class HostedAuthFlowTests: XCTestCase {
    func testOpenInstanceURLWithoutTenantOmitsTenantQuery() throws {
        let url = try XCTUnwrap(HostedAuthFlow.openInstanceURL())
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))

        XCTAssertEqual(components.scheme, "https")
        XCTAssertEqual(components.host, "control.longhouse.ai")
        XCTAssertEqual(components.path, "/auth/native/open-instance")
        XCTAssertNil(components.queryItems)
    }

    func testOpenInstanceURLWithTenantIncludesNormalizedTenant() throws {
        let url = try XCTUnwrap(HostedAuthFlow.openInstanceURL(tenant: "  David010 "))
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))

        XCTAssertEqual(components.queryItems, [URLQueryItem(name: "tenant", value: "david010")])
    }

    func testCallbackPayloadExtractsInstanceURLAndToken() throws {
        let callbackURL = try XCTUnwrap(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&instance_url=https%3A%2F%2Ftestuser.longhouse.ai&sso_token=abc123"
        ))

        let payload = try XCTUnwrap(HostedAuthFlow.callbackPayload(from: callbackURL))

        XCTAssertEqual(
            payload,
            HostedAuthCallbackPayload(
                tenant: "testuser",
                instanceURL: "https://testuser.longhouse.ai",
                ssoToken: "abc123",
                error: nil
            )
        )
    }

    func testCallbackPayloadExtractsHostedError() throws {
        let callbackURL = try XCTUnwrap(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&error=instance_not_found"
        ))

        let payload = try XCTUnwrap(HostedAuthFlow.callbackPayload(from: callbackURL))

        XCTAssertEqual(payload.error, "instance_not_found")
        XCTAssertEqual(payload.tenant, "testuser")
        XCTAssertNil(payload.ssoToken)
    }

    func testCallbackPayloadRejectsUnexpectedCallbackURL() throws {
        let callbackURL = try XCTUnwrap(URL(
            string: "https://control.longhouse.ai/auth/native/open-instance"
        ))

        XCTAssertNil(HostedAuthFlow.callbackPayload(from: callbackURL))
    }
}
