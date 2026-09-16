import Foundation
import UIKit

@MainActor
enum BugReportScreenCapture {
    static func captureJPEG() -> Data? {
        guard let window = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .filter({ $0.activationState == .foregroundActive })
            .flatMap(\.windows)
            .first(where: {
                $0.isKeyWindow
                    && !$0.isHidden
                    && $0.windowLevel == .normal
            })
        else { return nil }
        let renderer = UIGraphicsImageRenderer(bounds: window.bounds)
        let image = renderer.image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        return image.jpegData(compressionQuality: 0.78)
    }

    static func previewImage(from data: Data?) -> UIImage? {
        guard let data else { return nil }
        return UIImage(data: data)
    }
}

@MainActor
enum BugReportContext {
    static func timeline(serverURL: String) -> Data {
        let diagnostics = ClientDiagnosticsReporter.shared.snapshotEntries(sessionId: nil, limit: 100).map {
            var entry: [String: Any] = [
                "at_ms": $0.at_ms,
                "stage": $0.stage,
            ]
            if let detail = $0.detail { entry["detail"] = detail }
            if let sessionID = $0.session_id { entry["session_id"] = sessionID }
            return entry
        }
        let context: [String: Any] = [
            "surface": "timeline",
            "server_url": serverURL,
            "captured_at": ISO8601DateFormatter().string(from: Date()),
            "diagnostics": diagnostics,
            "app_build": (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "unknown",
        ]
        return (try? JSONSerialization.data(withJSONObject: context, options: [.sortedKeys])) ?? Data("{}".utf8)
    }
}
