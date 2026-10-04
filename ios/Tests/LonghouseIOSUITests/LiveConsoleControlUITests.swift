import CoreGraphics
import Foundation
import XCTest

/// Real-client control proof against a disposable hidden QA OMP Console session.
///
/// The driver creates a real OMP Console session with explicit QA provenance
/// (provider `omp`, origin `console`, and a hidden QA launch surface), supplies
/// its Runtime Host URL/token/session through TEST_RUNNER_LONGHOUSE_LIVE_CONTROL_*
/// variables, and imports one operator-supplied image into a disposable
/// simulator Photos library. This test never uses a fixture client or a
/// synthetic Shadow run: the production composer sends the idle message, sends
/// a mid-turn steer, observes a queued message from the Machine Agent sender,
/// then sends the imported image to the real OMP Console turn.
@MainActor
final class LiveConsoleControlUITests: XCTestCase {
    private static let timeout: TimeInterval = 180
    private static let uiTimeout: TimeInterval = 45

    private struct ProofFailure: Error, CustomStringConvertible {
        let description: String
    }

    private struct Configuration {
        let serverURL: URL
        let authToken: String
        let sessionID: String
        let thinkPrompt: String?
        let photoPrompt: String?
        let prefix: String
    }

    private struct SessionProvenance {
        let provider: String
        let originKind: String
        let launchActor: String?
        let launchSurface: String
        let hiddenFromDefaultTimeline: Bool
    }

    private struct RunMarkers {
        let idle: String
        let think: String
        let steer: String
        let queued: String
        let photo: String
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testLiveConsoleControlAndPhoto() async throws {
        #if !targetEnvironment(simulator)
        throw ProofFailure(description: "Live Console control proof is simulator-only")
        #else
        let configuration = try configuration()
        let provenance = try await validateSessionProvenance(configuration)
        let markers = RunMarkers(
            idle: marker(configuration.prefix, "IDLE"),
            think: marker(configuration.prefix, "THINK"),
            steer: marker(configuration.prefix, "STEER"),
            queued: marker(configuration.prefix, "QUEUED"),
            photo: marker(configuration.prefix, "PHOTO")
        )
        let app = XCUIApplication()
        app.launchEnvironment = [
            "LONGHOUSE_HEADLESS_SERVER_URL": configuration.serverURL.absoluteString,
            "LONGHOUSE_HEADLESS_AUTH_TOKEN": configuration.authToken,
            "LONGHOUSE_HEADLESS_OPEN_SESSION": configuration.sessionID,
        ]
        defer { app.terminate() }

        app.launch()
        // A newly created Console session is intentionally rendered as the
        // native empty state; WebKit mounts only after the first event exists.
        // Assert the real composer first, then require the WKWebView after the
        // served idle reply below.
        let composer = app.textFields["session-chat-composer"]
        let send = app.buttons["session-chat-send"]
        XCTAssertTrue(composer.waitForExistence(timeout: Self.uiTimeout), "real composer did not load")
        XCTAssertTrue(send.waitForExistence(timeout: Self.uiTimeout), "real send control did not load")

        try await sendFromIOS(
            app: app,
            composer: composer,
            send: send,
            text: "Reply with exactly \(markers.idle) and nothing else.",
            expectedLabel: "Send reply"
        )
        try await waitForEvents(
            configuration,
            requiringUser: markers.idle,
            assistant: markers.idle,
            timeout: Self.timeout,
            description: "idle SEND reply"
        )
        try waitForRenderedAssistant(
            app,
            marker: markers.idle,
            timeout: Self.uiTimeout,
            description: "idle SEND assistant reply in WKWebView"
        )
        attachScreenshot(app, name: "live-omp-idle-send")

        // A request to "think for N seconds" is answered in about two seconds
        // by a fast model, and a long enumeration is refused just as fast; both
        // end the turn before the queue and STEER steps. A long piece of writing
        // keeps the turn honestly busy without a tool.
        let thinkPrompt = configuration.thinkPrompt
            ?? "Without running any tools or commands, write an original short story of about 3000 words about a lighthouse keeper who restores an old clock, then end with exactly \(markers.think)."
        try await sendFromIOS(
            app: app,
            composer: composer,
            send: send,
            text: ensureMarker(thinkPrompt, markers.think),
            expectedLabel: "Send reply"
        )
        let thinkingObservation = try await waitForActivityState(
            configuration,
            equals: "thinking",
            timeout: Self.timeout,
            description: "served OMP Console thinking activity before queueing"
        )
        try waitForSendLabel(send, equals: "Send update mid-turn", timeout: Self.uiTimeout)

        let queueRequestID = "ios-live-machine-\(UUID().uuidString)"
        let queuedResponse = try await postMachineInput(
            configuration,
            text: "At the next turn boundary, reply with exactly \(markers.queued) and nothing else.",
            intent: "queue",
            clientRequestID: queueRequestID
        )
        guard queuedResponse["outcome"] as? String == "queued" else {
            throw ProofFailure(description: "Machine Agent sender did not receive a queued outcome: \(queuedResponse)")
        }
        try waitForQueuedElsewhereIndicator(app, timeout: Self.uiTimeout)
        let steerObservation = try await waitForActivityState(
            configuration,
            equals: "thinking",
            timeout: Self.uiTimeout,
            description: "served OMP Console thinking activity immediately before iOS STEER"
        )
        try await sendFromIOS(
            app: app,
            composer: composer,
            send: send,
            text: "Stop waiting at the next safe boundary and reply with exactly \(markers.steer) and nothing else.",
            expectedLabel: "Send update mid-turn"
        )
        try await waitForEvents(
            configuration,
            requiringUser: markers.steer,
            assistant: markers.steer,
            timeout: Self.timeout,
            description: "think-time STEER reply"
        )
        try waitForRenderedAssistant(
            app,
            marker: markers.steer,
            timeout: Self.uiTimeout,
            description: "think-time STEER assistant reply in WKWebView"
        )
        try await waitForEvents(
            configuration,
            requiringUser: markers.queued,
            assistant: markers.queued,
            timeout: Self.timeout,
            description: "sender-aware queued reply"
        )
        try waitForRenderedAssistant(
            app,
            marker: markers.queued,
            timeout: Self.uiTimeout,
            description: "sender-aware queued assistant reply in WKWebView"
        )
        attachScreenshot(app, name: "live-omp-steer-and-queued")

        try waitForSendLabel(send, equals: "Send reply", timeout: Self.uiTimeout)
        let actions = app.buttons["session-chat-compose-actions"]
        XCTAssertTrue(actions.waitForExistence(timeout: 10), "composer action menu did not return after the turn")
        actions.tap()
        let attach = app.buttons["session-chat-attach"]
        XCTAssertTrue(attach.waitForExistence(timeout: 10), "OMP Console did not advertise image attachments while idle")
        attach.tap()
        try chooseRealPhoto(in: app)
        XCTAssertTrue(
            app.descendants(matching: .any)["session-chat-attachment-tray"].waitForExistence(timeout: 15),
            "PhotosPicker selection did not reach the production attachment tray"
        )
        attachScreenshot(app, name: "live-omp-photo-selected")

        let photoPrompt = configuration.photoPrompt
            ?? "Inspect the attached photo and reply with exactly \(markers.photo) followed by one short description."
        composer.tap()
        composer.typeText(ensureMarker(photoPrompt, markers.photo))
        XCTAssertTrue(send.isEnabled, "photo SEND control was not enabled")
        send.tap()
        try await waitForEvents(
            configuration,
            requiringUser: markers.photo,
            assistant: markers.photo,
            requireAttachment: true,
            timeout: Self.timeout,
            description: "real photo SEND reply"
        )
        try waitForRenderedAssistant(
            app,
            marker: markers.photo,
            timeout: Self.uiTimeout,
            description: "real photo SEND assistant reply in WKWebView"
        )
        attachScreenshot(app, name: "live-omp-photo-reply")

        let evidence: [String: Any] = [
            "proof": "real-ios-omp-console-control",
            "session_id": configuration.sessionID,
            "provider": provenance.provider,
            "surface": "ios",
            "served_origin_kind": provenance.originKind,
            "served_launch_actor": provenance.launchActor ?? "unspecified",
            "served_launch_surface": provenance.launchSurface,
            "served_hidden_from_default_timeline": provenance.hiddenFromDefaultTimeline,
            "provenance_validated_before_app_launch": true,
            "idle_send": true,
            "think_time_steer": true,
            "served_thinking_state_before_queue": thinkingObservation,
            "served_thinking_state_before_steer": steerObservation,
            "served_thinking_state": true,
            "queued_from_other_sender": true,
            "photo_send": true,
            "markers": [markers.idle, markers.think, markers.steer, markers.queued, markers.photo],
        ]
        let data = try JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "live-omp-control-proof-evidence.json"
        attachment.lifetime = .keepAlways
        add(attachment)
        #endif
    }

    private func validateSessionProvenance(_ configuration: Configuration) async throws -> SessionProvenance {
        let url = configuration.serverURL
            .appendingPathComponent("api/agents/sessions")
            .appendingPathComponent(configuration.sessionID)
            .appendingPathComponent("workspace")
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 20
        request.setValue(configuration.authToken, forHTTPHeaderField: "X-Agents-Token")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ProofFailure(
                description: "live OMP provenance read failed before app launch: HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)"
            )
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ProofFailure(description: "live OMP provenance read returned non-object JSON")
        }
        guard let session = object["session"] as? [String: Any] else {
            throw ProofFailure(description: "live OMP provenance response omitted canonical session envelope")
        }
        guard let servedID = session["id"] as? String, servedID == configuration.sessionID else {
            throw ProofFailure(description: "live OMP provenance response did not identify the requested session")
        }
        guard let provider = (session["provider"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
              provider == "omp" else {
            throw ProofFailure(description: "live iOS proof target is not an OMP session")
        }
        let originKind = (session["origin_kind"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let sessionState = session["session_state"] as? [String: Any]
        let stateMode = (sessionState?["mode"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard originKind == "console" || stateMode == "console" else {
            throw ProofFailure(description: "live iOS proof target is not Console origin provenance")
        }
        let launchActor = (session["launch_actor"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let launchSurface = (session["launch_surface"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard let launchSurface,
              Set(["qa", "test", "e2e", "product-e2e", "ci"]).contains(launchSurface) else {
            throw ProofFailure(description: "live iOS proof target is not an explicit hidden QA launch surface")
        }
        guard let hidden = session["hidden_from_default_timeline"] as? Bool else {
            throw ProofFailure(description: "live iOS proof target omitted hidden-launch provenance")
        }
        guard hidden else {
            throw ProofFailure(description: "live iOS proof target is not hidden from the default timeline")
        }
        return SessionProvenance(
            provider: provider,
            originKind: originKind ?? stateMode ?? "console",
            launchActor: launchActor,
            launchSurface: launchSurface,
            hiddenFromDefaultTimeline: hidden
        )
    }

    private func configuration() throws -> Configuration {
        let environment = ProcessInfo.processInfo.environment
        let driverRequired = environment["LONGHOUSE_LIVE_CONTROL_REQUIRED"] == "1"
        func required(_ suffix: String) throws -> String {
            guard let value = environment["LONGHOUSE_LIVE_CONTROL_\(suffix)"]?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !value.isEmpty else {
                if driverRequired {
                    throw ProofFailure(description: "Missing LONGHOUSE_LIVE_CONTROL_\(suffix)")
                }
                throw XCTSkip("Live OMP control proof is not requested in this UI suite")
            }
            return value
        }
        let rawURL = try required("SERVER_URL")
        guard let serverURL = URL(string: rawURL),
              ["http", "https"].contains(serverURL.scheme?.lowercased() ?? ""),
              serverURL.host != nil,
              serverURL.query == nil,
              serverURL.fragment == nil else {
            throw ProofFailure(description: "LONGHOUSE_LIVE_CONTROL_SERVER_URL must be an HTTP(S) Runtime Host URL")
        }
        let authToken = try required("AUTH_TOKEN")
        let sessionID = try required("SESSION_ID")
        guard UUID(uuidString: sessionID) != nil else {
            throw ProofFailure(description: "LONGHOUSE_LIVE_CONTROL_SESSION_ID must be a UUID")
        }
        let prefix = (environment["LONGHOUSE_LIVE_CONTROL_MARKER_PREFIX"] ?? "ios-omp-")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return Configuration(
            serverURL: serverURL,
            authToken: authToken,
            sessionID: sessionID,
            thinkPrompt: environment["LONGHOUSE_LIVE_CONTROL_THINK_PROMPT"],
            photoPrompt: environment["LONGHOUSE_LIVE_CONTROL_PHOTO_PROMPT"],
            prefix: prefix.isEmpty ? "ios-omp-" : prefix
        )
    }

    private func marker(_ prefix: String, _ suffix: String) -> String {
        "\(prefix)\(suffix)-\(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(10))"
    }

    private func ensureMarker(_ prompt: String, _ marker: String) -> String {
        prompt.contains(marker) ? prompt : "\(prompt) Reply with \(marker) in the final answer."
    }

    private func sendFromIOS(
        app: XCUIApplication,
        composer: XCUIElement,
        send: XCUIElement,
        text: String,
        expectedLabel: String
    ) async throws {
        try waitForSendLabel(send, equals: expectedLabel, timeout: Self.uiTimeout)
        composer.tap()
        composer.typeText(text)
        XCTAssertTrue(send.isEnabled, "iOS send control was disabled after entering text")
        send.tap()
        try await Task.sleep(nanoseconds: 250_000_000)
        let optimisticRow = app.staticTexts.matching(NSPredicate(format: "label == %@", text)).firstMatch
        guard optimisticRow.waitForExistence(timeout: 5) else {
            throw ProofFailure(description: "iOS optimistic input row did not render")
        }
    }

    private func waitForSendLabel(_ send: XCUIElement, equals label: String, timeout: TimeInterval) throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if send.exists && send.label == label { return }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        throw ProofFailure(description: "expected iOS send label \(label.debugDescription), got \(send.label.debugDescription)")
    }
    private func waitForRenderedAssistant(
        _ app: XCUIApplication,
        marker: String,
        timeout: TimeInterval,
        description: String
    ) throws {
        let webView = app.webViews.firstMatch
        guard webView.waitForExistence(timeout: min(timeout, Self.uiTimeout)) else {
            throw ProofFailure(description: "\(description): production WKWebView transcript did not appear")
        }
        let predicate = NSPredicate(format: "label BEGINSWITH %@", marker)
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let matches = webView.staticTexts.matching(predicate)
            let labels = (0..<matches.count).compactMap { index -> String? in
                let label = matches.element(boundBy: index).label.trimmingCharacters(in: .whitespacesAndNewlines)
                return label.hasPrefix(marker) ? label : nil
            }
            if labels.count == 1 {
                return
            }
            if labels.count > 1 {
                throw ProofFailure(description: "\(description): duplicate marker-prefixed WebKit replies")
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        throw ProofFailure(description: "\(description): marker was served but never rendered in WebKit")
    }

    private func waitForQueuedElsewhereIndicator(_ app: XCUIApplication, timeout: TimeInterval) throws {
        let indicator = app.descendants(matching: .any)["session-chat-queued-indicator"]
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if indicator.exists && indicator.label.contains("another sender") { return }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        throw ProofFailure(description: "iOS never rendered the sender-aware queued indicator")
    }

    private func chooseRealPhoto(in app: XCUIApplication) throws {
        let pickerApplications: [(String, XCUIApplication)] = [
            ("PhotosUIService", XCUIApplication(bundleIdentifier: "com.apple.PhotosUIService")),
            ("PhotosViewService", XCUIApplication(bundleIdentifier: "com.apple.PhotosViewService")),
            ("host app", app),
        ]
        for (name, picker) in pickerApplications {
            let photo = picker.images.matching(identifier: "PXGGridLayout-Info").firstMatch
            guard photo.waitForExistence(timeout: 8) else { continue }
            guard photo.isEnabled, photo.frame.width > 0, photo.frame.height > 0 else {
                throw ProofFailure(description: "\(name) PhotosPicker photo was not enabled or had no surfaced geometry")
            }
            photo.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            let done = picker.buttons["Done"]
            let deadline = Date().addingTimeInterval(8)
            while Date() < deadline {
                if done.exists && done.isEnabled && done.isHittable {
                    done.tap()
                    return
                }
                RunLoop.current.run(until: Date().addingTimeInterval(0.25))
            }
            throw ProofFailure(description: "\(name) PhotosPicker did not expose an enabled Done action")
        }
        throw ProofFailure(description: "real photo imported by the driver was not visible in PhotosPicker")
    }

    private func postMachineInput(
        _ configuration: Configuration,
        text: String,
        intent: String,
        clientRequestID: String
    ) async throws -> [String: Any] {
        let url = configuration.serverURL
            .appendingPathComponent("api/agents/sessions")
            .appendingPathComponent(configuration.sessionID)
            .appendingPathComponent("input")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(configuration.authToken, forHTTPHeaderField: "X-Agents-Token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "text": text,
            "intent": intent,
            "client_request_id": clientRequestID,
        ])
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw ProofFailure(description: "Machine Agent queued input was refused: HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)")
        }
        guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ProofFailure(description: "Machine Agent queued input returned non-object JSON")
        }
        return value
    }

    private func fetchEvents(_ configuration: Configuration) async throws -> [[String: Any]] {
        var components = URLComponents(
            url: configuration.serverURL
                .appendingPathComponent("api/agents/sessions")
                .appendingPathComponent(configuration.sessionID)
                .appendingPathComponent("events"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(name: "anchor", value: "tail"),
            URLQueryItem(name: "limit", value: "200"),
        ]
        guard let url = components?.url else { throw ProofFailure(description: "could not build event URL") }
        var request = URLRequest(url: url)
        request.setValue(configuration.authToken, forHTTPHeaderField: "X-Agents-Token")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ProofFailure(description: "event read failed: HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)")
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let events = object["events"] as? [[String: Any]] else {
            throw ProofFailure(description: "event read returned no events array")
        }
        return events
    }

    private func waitForActivityState(
        _ configuration: Configuration,
        equals expected: String,
        timeout: TimeInterval,
        description: String
    ) async throws -> String {
        let url = configuration.serverURL
            .appendingPathComponent("api/agents/sessions")
            .appendingPathComponent(configuration.sessionID)
            .appendingPathComponent("workspace")
        var request = URLRequest(url: url)
        request.setValue(configuration.authToken, forHTTPHeaderField: "X-Agents-Token")
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                throw ProofFailure(description: "activity state read failed: HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)")
            }
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw ProofFailure(description: "activity state read returned non-object JSON")
            }
            guard let session = object["session"] as? [String: Any],
                  let stateFacts = session["session_state"] as? [String: Any],
                  let activity = stateFacts["activity"] as? [String: Any],
                  let state = activity["state"] as? String else {
                throw ProofFailure(description: "workspace response omitted canonical session_state.activity.state")
            }
            if state == expected {
                return state
            }
            try await Task.sleep(nanoseconds: 1_000_000_000)
        }
        throw ProofFailure(description: "timed out waiting for \(description)")
    }

    private func waitForEvents(
        _ configuration: Configuration,
        requiringUser userMarker: String,
        assistant assistantMarker: String,
        requireAttachment: Bool = false,
        timeout: TimeInterval,
        description: String
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let events = try await fetchEvents(configuration)
            let users = events.filter { event in
                event["role"] as? String == "user" && eventText(event).contains(userMarker)
            }
            let assistants = events.filter { event in
                event["role"] as? String == "assistant" && eventText(event).contains(assistantMarker)
            }
            let attachmentOK = !requireAttachment || users.contains { event in
                if let mediaRefs = event["media_refs"] as? [[String: Any]], !mediaRefs.isEmpty {
                    return true
                }
                if let attachments = event["attachments"] as? [[String: Any]], !attachments.isEmpty {
                    return true
                }
                return false
            }
            if users.count == 1 && assistants.count == 1 && attachmentOK { return }
            try await Task.sleep(nanoseconds: 1_000_000_000)
        }
        throw ProofFailure(description: "timed out waiting for real \(description)")
    }

    private func eventText(_ event: [String: Any]) -> String {
        if let text = event["content_text"] as? String { return text }
        if let text = event["text"] as? String { return text }
        return "\(event["message"] ?? "")"
    }

    private func attachScreenshot(_ app: XCUIApplication, name: String) {
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = name
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
