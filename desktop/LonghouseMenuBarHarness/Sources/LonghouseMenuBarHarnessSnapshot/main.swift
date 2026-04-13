import Foundation
import LonghouseMenuBarCore

@main
struct LonghouseMenuBarHarnessSnapshot {
    @MainActor
    static func main() throws {
        let config = try HarnessRuntimeConfig.parse(arguments: Array(CommandLine.arguments.dropFirst()))
        guard let outputURL = config.outputURL else {
            throw SnapshotSourceError.invalidArguments("Pass --output <png-path> when rendering a snapshot")
        }

        let snapshot = try config.source.load()
        let actionSink = SpyHealthActionSink(logURL: config.actionLogURL, uiURL: config.uiURL)
        try SnapshotRenderer.renderPNG(snapshot: snapshot, actionSink: actionSink, outputURL: outputURL)
        print(outputURL.path)
    }
}
