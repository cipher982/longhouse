import Foundation

/// Per-build provenance for the iOS app.
///
/// Loaded from the bundled `build-identity.json` that the pre-build script
/// copies out of `.build/build-identity.json` before Xcode's Copy Bundle
/// Resources phase. Mirrors `server/zerg/build_info.py` and
/// `engine/src/build_identity.rs` so every surface formats versions the
/// same way: `0.2.0 (b672fcca)` for release builds, `0.2.0-dev+b672fcca`
/// for dev, `0.2.0-dev+b672fcca.dirty` for dev with local edits.
public struct BuildIdentity: Codable, Equatable, Sendable {
    public let version: String
    public let commit: String
    public let commitShort: String
    public let dirty: Bool
    public let builtAt: String
    public let channel: String

    public init(
        version: String,
        commit: String,
        commitShort: String,
        dirty: Bool,
        builtAt: String,
        channel: String
    ) {
        self.version = version
        self.commit = commit
        self.commitShort = commitShort
        self.dirty = dirty
        self.builtAt = builtAt
        self.channel = channel
    }

    public enum CodingKeys: String, CodingKey {
        case version
        case commit
        case commitShort = "commit_short"
        case dirty
        case builtAt = "built_at"
        case channel
    }

    public var qualifiedVersion: String {
        switch channel {
        case "release":
            return "\(version) (\(commitShort))"
        default:
            let suffix = dirty ? "\(commitShort).dirty" : commitShort
            return "\(version)-dev+\(suffix)"
        }
    }
}

public enum BuildIdentityLoader {
    public enum LoadError: Error, Equatable {
        case resourceMissing
        case decodeFailed(String)
        case invalidPayload(String)
    }

    private static let resourceName = "build-identity"
    private static let resourceExtension = "json"
    private static let allowedChannels: Set<String> = ["dev", "release"]

    /// Decode a build identity from raw JSON bytes. Split out from
    /// `load(from:)` so tests can exercise parsing without a real bundle.
    public static func decode(_ data: Data) -> Result<BuildIdentity, LoadError> {
        let decoder = JSONDecoder()
        let identity: BuildIdentity
        do {
            identity = try decoder.decode(BuildIdentity.self, from: data)
        } catch {
            return .failure(.decodeFailed(String(describing: error)))
        }

        if identity.version.isEmpty
            || identity.commit.isEmpty
            || identity.commitShort.isEmpty
            || identity.builtAt.isEmpty
        {
            return .failure(.invalidPayload("empty required field"))
        }
        if !allowedChannels.contains(identity.channel) {
            return .failure(.invalidPayload("unknown channel: \(identity.channel)"))
        }
        return .success(identity)
    }

    /// Load from a concrete Bundle. Returns `.resourceMissing` if the
    /// bundled JSON is absent — that's a build bug, not a runtime case to
    /// paper over. UI surfaces should show "build identity missing" rather
    /// than a fake version string.
    public static func load(from bundle: Bundle) -> Result<BuildIdentity, LoadError> {
        guard let url = bundle.url(forResource: resourceName, withExtension: resourceExtension) else {
            return .failure(.resourceMissing)
        }
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            return .failure(.decodeFailed(String(describing: error)))
        }
        return decode(data)
    }

    /// Convenience for production code. Tests should use `load(from:)`
    /// with a specific bundle to stay hermetic.
    public static func loadFromMainBundle() -> Result<BuildIdentity, LoadError> {
        load(from: .main)
    }
}
