import Foundation
import Testing
@testable import Longhouse

struct HostedAuthFlowTests {
    @Test
    func openInstanceURLWithoutTenantOmitsTenantQuery() throws {
        let url = try #require(HostedAuthFlow.openInstanceURL())
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.scheme == "https")
        #expect(components.host == "control.longhouse.ai")
        #expect(components.path == "/auth/native/open-instance")
        #expect(components.queryItems == nil)
    }

    @Test
    func openInstanceURLWithTenantIncludesNormalizedTenant() throws {
        let url = try #require(HostedAuthFlow.openInstanceURL(tenant: "  Demo "))
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.queryItems == [URLQueryItem(name: "tenant", value: "demo")])
    }

    @Test
    func openInstanceURLIncludesHandoffVerifier() throws {
        let url = try #require(HostedAuthFlow.openInstanceURL(tenant: "Demo", handoffVerifier: " verifier-123 "))
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.queryItems == [
            URLQueryItem(name: "tenant", value: "demo"),
            URLQueryItem(name: "tenant_state", value: "verifier-123"),
        ])
    }

    @Test
    func callbackPayloadExtractsInstanceURLAndRuntimeToken() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&instance_url=https%3A%2F%2Ftestuser.longhouse.ai&runtime_token=abc123&tenant_state=state123"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(
            payload == HostedAuthCallbackPayload(
                tenant: "testuser",
                instanceURL: "https://testuser.longhouse.ai",
                code: nil,
                runtimeToken: "abc123",
                tenantState: "state123",
                error: nil
            )
        )
    }

    @Test
    func callbackPayloadAcceptsLegacySSOTokenQueryName() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&instance_url=https%3A%2F%2Ftestuser.longhouse.ai&sso_token=abc123"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(payload.runtimeToken == "abc123")
    }

    @Test
    func callbackPayloadExtractsHandoffCode() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&instance_url=https%3A%2F%2Ftestuser.longhouse.ai&code=handoff123"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(payload.code == "handoff123")
        #expect(payload.runtimeToken == nil)
    }

    @Test
    func callbackPayloadExtractsHostedError() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&error=instance_not_found"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(payload.error == "instance_not_found")
        #expect(payload.tenant == "testuser")
        #expect(payload.runtimeToken == nil)
    }

    @Test
    func callbackPayloadRejectsUnexpectedCallbackURL() throws {
        let callbackURL = try #require(URL(
            string: "https://control.longhouse.ai/auth/native/open-instance"
        ))

        #expect(HostedAuthFlow.callbackPayload(from: callbackURL) == nil)
    }
}
