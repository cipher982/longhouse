import Foundation

enum LonghouseResourceLocator {
    private static let coreBundleName = "LonghouseMenuBarHarness_LonghouseMenuBarCore.bundle"

    static func coreBundle() -> Bundle? {
        let appCandidates = [
            Bundle.main.resourceURL?.appendingPathComponent(coreBundleName, isDirectory: true),
            Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/\(coreBundleName)", isDirectory: true),
        ]

        for candidate in appCandidates {
            guard let candidate else { continue }
            if let bundle = Bundle(url: candidate) {
                return bundle
            }
        }

        if let moduleURL = Bundle.module.resourceURL,
           let bundle = Bundle(url: moduleURL) {
            return bundle
        }

        return nil
    }

    static func url(forResource name: String, withExtension ext: String) -> URL? {
        coreBundle()?.url(forResource: name, withExtension: ext)
    }
}
