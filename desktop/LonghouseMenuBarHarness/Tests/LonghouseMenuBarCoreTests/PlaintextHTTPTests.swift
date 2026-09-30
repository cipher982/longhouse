import Foundation
import Testing

@testable import LonghouseMenuBarCore

/// The plaintext-http rule, against the case list the Python, Rust, macOS
/// Desktop and iOS tests all read (`schemas/plaintext-http-vectors.json`).
struct PlaintextHTTPTests {
    struct Vectors: Decodable {
        struct Host: Decodable, CustomTestStringConvertible {
            let host: String
            let `class`: String
            var testDescription: String { host.isEmpty ? "<empty>" : host }
        }

        struct URLCase: Decodable, CustomTestStringConvertible {
            let url: String
            let allowInsecureHttp: Bool
            let verdict: String
            var testDescription: String { "\(url.debugDescription) optIn=\(allowInsecureHttp)" }
        }

        let hosts: [Host]
        let urls: [URLCase]
    }

    static let vectors: Vectors = {
        // Five levels up from this file is the repository root.
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { root.deleteLastPathComponent() }
        let data = try! Data(contentsOf: root.appendingPathComponent("schemas/plaintext-http-vectors.json"))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(Vectors.self, from: data)
    }()

    @Test
    func theSharedVectorsWereRead() {
        #expect(Self.vectors.hosts.count > 40)
        #expect(Self.vectors.urls.count > 40)
    }

    @Test(arguments: vectors.hosts)
    func hostClassificationMatchesTheSharedVectors(_ vector: Vectors.Host) {
        #expect(PlaintextHTTP.classify(host: vector.host).rawValue == vector.`class`)
    }

    @Test(arguments: vectors.urls)
    func urlVerdictsMatchTheSharedVectors(_ vector: Vectors.URLCase) {
        let outcome = PlaintextHTTP.check(vector.url, allowInsecureHTTP: vector.allowInsecureHttp)
        #expect(outcome.rawValue == vector.verdict)
    }

    @Test
    func theTailscaleV4EdgesAreExact() {
        #expect(PlaintextHTTP.classify(host: "100.63.255.255") == .public)
        #expect(PlaintextHTTP.classify(host: "100.64.0.1") == .tailscale)
        #expect(PlaintextHTTP.classify(host: "100.127.255.255") == .tailscale)
        #expect(PlaintextHTTP.classify(host: "100.128.0.0") == .public)
    }

    @Test
    func aLANRefusalNamesTheOptInAndAPublicOneDoesNot() {
        let lan = PlaintextHTTP.refusalMessage("http://192.168.1.20:8080", outcome: .refusedLAN)
        #expect(lan.contains(PlaintextHTTP.optInFlag))
        #expect(lan.contains(PlaintextHTTP.optInEnvironment))
        #expect(lan.contains("Tailscale"))

        let publicRefusal = PlaintextHTTP.refusalMessage("http://demo.longhouse.ai", outcome: .refusedPublic)
        #expect(!publicRefusal.contains(PlaintextHTTP.optInFlag))
        #expect(publicRefusal.contains("https://"))
    }

    @Test
    func theWarningIsOneLineNamingTheAddressAndTheOptIn() {
        let warning = PlaintextHTTP.insecureWarning(" http://192.168.1.20:8080 ")
        #expect(!warning.contains("\n"))
        #expect(warning.contains("http://192.168.1.20:8080"))
        #expect(warning.contains(PlaintextHTTP.optInFlag))
    }

    /// The address is judged before any request is made, so a refused one fails
    /// the stream with the refusal itself and never sends the device token.
    private func firstFailure(runtimeUrl: String, allowInsecureHttp: Bool?) async throws -> String? {
        let tokenURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-plaintext-token-\(UUID().uuidString)")
        try Data("test-token".utf8).write(to: tokenURL)
        defer { try? FileManager.default.removeItem(at: tokenURL) }

        let stream = SessionProjectionStream.projections(
            connection: RealtimeConnectionSnapshot(
                runtimeUrl: runtimeUrl,
                machineName: nil,
                tokenPath: tokenURL.path,
                allowInsecureHttp: allowInsecureHttp
            ),
            sessionIds: ["session-1"]
        )
        return await withTaskGroup(of: String?.self) { group -> String? in
            group.addTask {
                for await event in stream {
                    if case .failed(let message) = event { return message }
                }
                return nil
            }
            group.addTask {
                try? await Task.sleep(for: .seconds(3))
                return nil
            }
            let result = await group.next() ?? nil
            group.cancelAll()
            return result
        }
    }

    @Test
    func theProjectionStreamRefusesAPublicOrUnopenedLANAddress() async throws {
        let lan = try await firstFailure(runtimeUrl: "http://192.168.1.20:1", allowInsecureHttp: nil)
        #expect(lan?.contains("Refusing plaintext") == true)
        #expect(lan?.contains(PlaintextHTTP.optInFlag) == true)

        let publicHost = try await firstFailure(runtimeUrl: "http://demo.longhouse.ai", allowInsecureHttp: true)
        #expect(publicHost?.contains("Refusing plaintext") == true)
    }
}
