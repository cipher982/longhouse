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
    func openInstanceURLIncludesPKCEChallenge() throws {
        let url = try #require(
            HostedAuthFlow.openInstanceURL(
                tenant: "Demo",
                handoffVerifier: "state-123",
                codeChallenge: "challenge-123"
            )
        )
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))

        #expect(components.queryItems == [
            URLQueryItem(name: "tenant", value: "demo"),
            URLQueryItem(name: "tenant_state", value: "state-123"),
            URLQueryItem(name: "code_challenge", value: "challenge-123"),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
        ])
    }

    @Test
    func codeChallengeUsesRFC7636S256Encoding() {
        let verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        #expect(
            HostedAuthFlow.codeChallenge(for: verifier)
                == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )
    }

    @Test
    func validatedInstanceURLRequiresExpectedHostedOrigin() {
        #expect(
            HostedAuthFlow.validatedInstanceURL(
                "https://testuser.longhouse.ai/",
                tenant: "testuser",
                expectedServerURL: nil
            ) == "https://testuser.longhouse.ai"
        )
        #expect(
            HostedAuthFlow.validatedInstanceURL(
                "https://attacker.example.test",
                tenant: "testuser",
                expectedServerURL: nil
            ) == nil
        )
        #expect(
            HostedAuthFlow.validatedInstanceURL(
                "https://other.longhouse.ai",
                tenant: "testuser",
                expectedServerURL: "https://testuser.longhouse.ai"
            ) == nil
        )
    }

    @Test
    func validatedInstanceURLRequiresCallbackTenant() {
        #expect(
            HostedAuthFlow.validatedInstanceURL(
                "https://testuser.longhouse.ai",
                tenant: nil,
                expectedServerURL: nil
            ) == nil
        )
    }

    @Test
    func callbackPayloadRejectsDuplicateSensitiveValues() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&tenant_state=one&tenant_state=two"
        ))

        #expect(HostedAuthFlow.callbackPayload(from: callbackURL) == nil)
    }

    @Test
    func callbackPayloadExtractsInstanceURLAndHandoffState() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&instance_url=https%3A%2F%2Ftestuser.longhouse.ai&tenant_state=state123"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(
            payload == HostedAuthCallbackPayload(
                tenant: "testuser",
                instanceURL: "https://testuser.longhouse.ai",
                code: nil,
                tenantState: "state123",
                error: nil
            )
        )
    }

    @Test
    func callbackPayloadIgnoresLegacyCredentialQueryValues() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&runtime_token=abc123&sso_token=old"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(payload.code == nil)
        #expect(payload.tenant == "testuser")
        #expect(payload.error == nil)
    }

    @Test
    func callbackPayloadExtractsHandoffCode() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&instance_url=https%3A%2F%2Ftestuser.longhouse.ai&code=handoff123"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(payload.code == "handoff123")
        #expect(payload.tenant == "testuser")
        #expect(payload.instanceURL == "https://testuser.longhouse.ai")
    }

    @Test
    func callbackPayloadExtractsHostedError() throws {
        let callbackURL = try #require(URL(
            string: "ai.longhouse.ios://auth-callback?tenant=testuser&error=instance_not_found"
        ))

        let payload = try #require(HostedAuthFlow.callbackPayload(from: callbackURL))

        #expect(payload.error == "instance_not_found")
        #expect(payload.tenant == "testuser")
        #expect(payload.code == nil)
    }

    @Test
    func callbackPayloadRejectsUnexpectedCallbackURL() throws {
        let callbackURL = try #require(URL(
            string: "https://control.longhouse.ai/auth/native/open-instance"
        ))

        #expect(HostedAuthFlow.callbackPayload(from: callbackURL) == nil)
    }
}
