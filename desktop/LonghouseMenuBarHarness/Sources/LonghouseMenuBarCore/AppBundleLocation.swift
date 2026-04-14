import Foundation

enum AppBundleLocation {
    static let canonicalBundlePath = "/Applications/Longhouse.app"

    static func unsupportedBundlePath(currentBundlePath: String) -> String? {
        let normalizedPath = URL(fileURLWithPath: currentBundlePath).standardizedFileURL.path
        guard normalizedPath.hasSuffix(".app") else {
            return nil
        }

        let bundleName = URL(fileURLWithPath: normalizedPath).lastPathComponent
        guard bundleName == "Longhouse.app" else {
            return nil
        }

        return normalizedPath == canonicalBundlePath ? nil : normalizedPath
    }

    static func currentUnsupportedBundlePath() -> String? {
        unsupportedBundlePath(currentBundlePath: Bundle.main.bundleURL.path)
    }
}
