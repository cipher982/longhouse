import Foundation

/// The repository root, found by walking up from a test source file until the
/// directory that holds `ios/XcodeHarness/project.yml`. Tests that read shared
/// fixtures (`tests/fixtures`, `config/`) use it instead of counting parent
/// directories, so moving a test into a feature folder cannot break them.
enum RepoRoot {
    static func url(filePath: String = #filePath) -> URL {
        var directory = URL(fileURLWithPath: filePath).deletingLastPathComponent()
        while directory.path != "/" {
            let marker = directory.appendingPathComponent("ios/XcodeHarness/project.yml")
            if FileManager.default.fileExists(atPath: marker.path) {
                return directory
            }
            directory = directory.deletingLastPathComponent()
        }
        preconditionFailure("no repository root above \(filePath)")
    }
}
