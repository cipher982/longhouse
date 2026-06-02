import Foundation
@preconcurrency import UIKit
@preconcurrency import UserNotifications
import WidgetKit

extension Notification.Name {
    static let longhouseAPNSDeviceTokenUpdated = Notification.Name("longhouse.apnsDeviceTokenUpdated")
    static let longhouseOpenSessionFromPush = Notification.Name("longhouse.openSessionFromPush")
}

enum LonghouseNotificationCategory {
    static let sessionAttention = "LONGHOUSE_SESSION_ATTENTION"
    static let openSessionAction = "LONGHOUSE_OPEN_SESSION"

    static func allCategories() -> Set<UNNotificationCategory> {
        let openSession = UNNotificationAction(
            identifier: openSessionAction,
            title: "Open session",
            options: [.foreground]
        )
        let attention = UNNotificationCategory(
            identifier: sessionAttention,
            actions: [openSession],
            intentIdentifiers: [],
            options: [.customDismissAction]
        )
        return [attention]
    }
}

enum PushNotificationStore {
    nonisolated(unsafe) private static let defaults = UserDefaults.standard
    private static let deviceTokenKey = "longhouse.apns.deviceToken"
    private static let pendingSessionKey = "longhouse.apns.pendingSession"
    private static let registrationSignatureKey = "longhouse.apns.registrationSignature"
    private static let registrationSyncedAtKey = "longhouse.apns.registrationSyncedAt"
    static let registrationRefreshInterval: TimeInterval = 6 * 60 * 60

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

    static func apnsDeviceRegistrationSignature(
        serverURL: String,
        deviceToken: String,
        pushEnvironment: String,
        appBuildId: String?,
        platform: String
    ) -> String {
        [
            serverURL.trimmingCharacters(in: .whitespacesAndNewlines),
            platform.trimmingCharacters(in: .whitespacesAndNewlines),
            pushEnvironment.trimmingCharacters(in: .whitespacesAndNewlines),
            appBuildId?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "",
            deviceToken.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
        ].joined(separator: "|")
    }

    static func shouldSyncAPNSDevice(signature: String, now: Date = Date()) -> Bool {
        guard defaults.string(forKey: registrationSignatureKey) == signature else {
            return true
        }
        guard let lastSyncedAt = defaults.object(forKey: registrationSyncedAtKey) as? Date else {
            return true
        }
        return now.timeIntervalSince(lastSyncedAt) >= registrationRefreshInterval
    }

    static func markAPNSDeviceSynced(signature: String, at date: Date = Date()) {
        defaults.set(signature, forKey: registrationSignatureKey)
        defaults.set(date, forKey: registrationSyncedAtKey)
    }

    static func clearAPNSDeviceSyncState() {
        defaults.removeObject(forKey: registrationSignatureKey)
        defaults.removeObject(forKey: registrationSyncedAtKey)
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

    static func clearPendingSessionID(_ sessionID: String) {
        let pending = defaults.string(forKey: pendingSessionKey)?.trimmingCharacters(in: .whitespacesAndNewlines)
        if pending == sessionID {
            defaults.removeObject(forKey: pendingSessionKey)
        }
    }

    static func reloadWidgetTimelines() {
        WidgetCenter.shared.reloadAllTimelines()
    }

    static func removeResolvedAttentionNotifications(activeSessionIDs: Set<String>) {
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { delivered in
            let resolvedIdentifiers = delivered.compactMap { notification -> String? in
                let content = notification.request.content
                let userInfo = content.userInfo
                let isAttentionAlert = content.categoryIdentifier == LonghouseNotificationCategory.sessionAttention
                    || userInfo["attention_state"] != nil
                guard isAttentionAlert, let sessionID = userInfo["session_id"] as? String else {
                    return nil
                }
                return activeSessionIDs.contains(sessionID) ? nil : notification.request.identifier
            }
            if !resolvedIdentifiers.isEmpty {
                center.removeDeliveredNotifications(withIdentifiers: resolvedIdentifiers)
            }
        }
    }

    static func removeDeliveredAttentionNotifications(sessionIDs: Set<String>) {
        guard !sessionIDs.isEmpty else { return }
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { delivered in
            let identifiers = delivered.compactMap { notification -> String? in
                let content = notification.request.content
                let userInfo = content.userInfo
                let isAttentionAlert = content.categoryIdentifier == LonghouseNotificationCategory.sessionAttention
                    || userInfo["attention_state"] != nil
                guard isAttentionAlert, let sessionID = userInfo["session_id"] as? String else {
                    return nil
                }
                return sessionIDs.contains(sessionID) ? notification.request.identifier : nil
            }
            if !identifiers.isEmpty {
                center.removeDeliveredNotifications(withIdentifiers: identifiers)
            }
        }
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
        UNUserNotificationCenter.current().setNotificationCategories(LonghouseNotificationCategory.allCategories())
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

    func application(
        _ application: UIApplication,
        didReceiveRemoteNotification userInfo: [AnyHashable: Any],
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        let event = userInfo["event"] as? String
        let attentionState = userInfo["attention_state"] as? String
        let resolvedSessionID = userInfo["session_id"] as? String
        Task {
            let result = await Self.handleBackgroundPushPayload(
                event: event,
                attentionState: attentionState,
                resolvedSessionID: resolvedSessionID
            )
            completionHandler(result)
        }
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
        switch response.actionIdentifier {
        case UNNotificationDismissActionIdentifier:
            return
        case UNNotificationDefaultActionIdentifier, LonghouseNotificationCategory.openSessionAction:
            Self.handlePushPayload(response.notification.request.content.userInfo)
        default:
            return
        }
    }

    private nonisolated static func handlePushPayload(_ userInfo: [AnyHashable: Any]) {
        if let sessionID = userInfo["session_id"] as? String, !sessionID.isEmpty {
            PushNotificationStore.reloadWidgetTimelines()
            PushNotificationStore.storePendingSessionID(sessionID)
        }
    }

    private nonisolated static func handleBackgroundPushPayload(
        event: String?,
        attentionState: String?,
        resolvedSessionID rawSessionID: String?
    ) async -> UIBackgroundFetchResult {
        guard event == "attention_resolved" || attentionState == "resolved" else {
            return .noData
        }

        let resolvedSessionID = rawSessionID?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let serverURL = SharedAuthStore.loadServerURL(), let api = LonghouseAPI(host: serverURL) else {
            if let resolvedSessionID, !resolvedSessionID.isEmpty {
                PushNotificationStore.removeDeliveredAttentionNotifications(sessionIDs: [resolvedSessionID])
            }
            PushNotificationStore.reloadWidgetTimelines()
            return .noData
        }

        do {
            let sessions = try await api.recentActiveSessions(limit: 40)
            let attentionIds = Set(sessions.filter(\.needsAttention).map(\.id))
            WidgetSessionSnapshotStore.save(sessions: sessions)
            PushNotificationStore.removeResolvedAttentionNotifications(activeSessionIDs: attentionIds)
            PushNotificationStore.reloadWidgetTimelines()
            return .newData
        } catch {
            if let resolvedSessionID, !resolvedSessionID.isEmpty {
                PushNotificationStore.removeDeliveredAttentionNotifications(sessionIDs: [resolvedSessionID])
                PushNotificationStore.reloadWidgetTimelines()
                return .noData
            }
            return .failed
        }
    }
}
