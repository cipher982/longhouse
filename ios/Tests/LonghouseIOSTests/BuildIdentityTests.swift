import Foundation
import Testing
@testable import Longhouse

struct BuildIdentityTests {
    private static let releasePayload = """
    {
      "version": "0.2.0",
      "commit": "b672fccae990c020de56139d38dcd9990bae7aa0",
      "commit_short": "b672fcca",
      "dirty": false,
      "built_at": "2026-04-21T18:03:12Z",
      "channel": "release"
    }
    """

    private static let dirtyDevPayload = """
    {
      "version": "0.2.0",
      "commit": "0f052998fefdba833f3d5f15404ac6d3e870cdb5",
      "commit_short": "0f052998",
      "dirty": true,
      "built_at": "2026-04-21T17:16:19Z",
      "channel": "dev"
    }
    """

    @Test
    func qualifiedVersionFormatsReleaseChannel() throws {
        let data = try #require(Self.releasePayload.data(using: .utf8))
        let identity: BuildIdentity
        switch BuildIdentityLoader.decode(data) {
        case .success(let value):
            identity = value
        case .failure(let err):
            Issue.record("expected decode success, got \(err)")
            return
        }
        #expect(identity.qualifiedVersion == "0.2.0 (b672fcca)")
    }

    @Test
    func qualifiedVersionFormatsDevDirty() throws {
        let data = try #require(Self.dirtyDevPayload.data(using: .utf8))
        switch BuildIdentityLoader.decode(data) {
        case .success(let identity):
            #expect(identity.qualifiedVersion == "0.2.0-dev+0f052998.dirty")
        case .failure(let err):
            Issue.record("expected decode success, got \(err)")
        }
    }

    @Test
    func qualifiedVersionFormatsDevClean() throws {
        // Same as dirty payload but with dirty=false — mirrors the clean dev
        // branch in the Python/Rust formatters.
        let clean = Self.dirtyDevPayload.replacingOccurrences(
            of: "\"dirty\": true",
            with: "\"dirty\": false"
        )
        let data = try #require(clean.data(using: .utf8))
        switch BuildIdentityLoader.decode(data) {
        case .success(let identity):
            #expect(identity.qualifiedVersion == "0.2.0-dev+0f052998")
        case .failure(let err):
            Issue.record("expected decode success, got \(err)")
        }
    }

    @Test
    func rejectsUnknownChannel() throws {
        let payload = Self.releasePayload.replacingOccurrences(
            of: "\"release\"",
            with: "\"nightly\""
        )
        let data = try #require(payload.data(using: .utf8))
        switch BuildIdentityLoader.decode(data) {
        case .success(let identity):
            Issue.record("expected failure, got \(identity)")
        case .failure(let err):
            if case .invalidPayload = err {
                // Expected: channel not in the allowed enum. Mirrors the
                // Python/Rust strict-channel checks so a malformed identity
                // can't quietly slip through on just one surface.
            } else {
                Issue.record("expected invalidPayload, got \(err)")
            }
        }
    }

    @Test
    func rejectsEmptyCommitShort() throws {
        let payload = Self.releasePayload.replacingOccurrences(
            of: "\"commit_short\": \"b672fcca\"",
            with: "\"commit_short\": \"\""
        )
        let data = try #require(payload.data(using: .utf8))
        switch BuildIdentityLoader.decode(data) {
        case .success(let identity):
            Issue.record("expected failure, got \(identity)")
        case .failure(let err):
            if case .invalidPayload = err {
                // Expected: empty required field
            } else {
                Issue.record("expected invalidPayload, got \(err)")
            }
        }
    }

    @Test
    func rejectsMalformedJson() throws {
        let data = try #require("not json".data(using: .utf8))
        switch BuildIdentityLoader.decode(data) {
        case .success(let identity):
            Issue.record("expected failure, got \(identity)")
        case .failure(let err):
            if case .decodeFailed = err {
                // Expected: JSONDecoder error bubbled up
            } else {
                Issue.record("expected decodeFailed, got \(err)")
            }
        }
    }
}
