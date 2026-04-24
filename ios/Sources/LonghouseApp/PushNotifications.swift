import Foundation
@preconcurrency import UIKit
@preconcurrency import UserNotifications

extension Notification.Name {
    static let longhouseAPNSDeviceTokenUpdated = Notification.Name("longhouse.apnsDeviceTokenUpdated")
    static let longhouseOpenSessionFromPush = Notification.Name("longhouse.openSessionFromPush")
}

enum PushNotificationStore {
    nonisolated(unsafe) private static let defaults = UserDefaults.standard
    private static let deviceTokenKey = "longhouse.apns.deviceToken"
    private static let pendingSessionKey = "longhouse.apns.pendingSession"

    static var pushEnvironment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    static var currentAppBuildID: String? {
        switch BuildIdentityLoader.loadFromMainBundle() {
        case .success(let identity):
            return identity.qualifiedVersion
        case .failure:
            return Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        }
    }

    static func saveDeviceToken(_ deviceToken: Data) -> String {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        defaults.set(token, forKey: deviceTokenKey)
        return token
    }

    static func storedDeviceToken() -> String? {
        let token = defaults.string(forKey: deviceTokenKey)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return token.isEmpty ? nil : token
    }

    static func storePendingSessionID(_ sessionID: String) {
        defaults.set(sessionID, forKey: pendingSessionKey)
        NotificationCenter.default.post(name: .longhouseOpenSessionFromPush, object: sessionID)
    }

    static func consumePendingSessionID() -> String? {
        let sessionID = defaults.string(forKey: pendingSessionKey)
        defaults.removeObject(forKey: pendingSessionKey)
        return sessionID?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @MainActor
    static func ensureAuthorizedAndRegister() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()

        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            UIApplication.shared.registerForRemoteNotifications()
            return true
        case .notDetermined:
            do {
                let granted = try await center.requestAuthorization(options: [.alert, .badge, .sound])
                if granted {
                    UIApplication.shared.registerForRemoteNotifications()
                }
                return granted
            } catch {
                return false
            }
        case .denied:
            return false
        @unknown default:
            return false
        }
    }
}

final class LonghousePushAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        if let userInfo = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
            Self.handlePushPayload(userInfo)
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        _ = PushNotificationStore.saveDeviceToken(deviceToken)
        NotificationCenter.default.post(name: .longhouseAPNSDeviceTokenUpdated, object: nil)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NSLog("Longhouse APNs registration failed: %@", error.localizedDescription)
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .badge]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        Self.handlePushPayload(response.notification.request.content.userInfo)
    }

    private nonisolated static func handlePushPayload(_ userInfo: [AnyHashable: Any]) {
        if let sessionID = userInfo["session_id"] as? String, !sessionID.isEmpty {
            PushNotificationStore.storePendingSessionID(sessionID)
        }
    }
}
