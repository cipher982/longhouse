import Foundation
import XCTest
import Vision

/// Read-only proof against a caller-created, hidden QA session, never a fixture.
/// Preflight requires ordered, exact durable assistant replies; accessibility and
/// screenshot pixels separately prove client rendering. Ask the provider to
/// concatenate marker fragments so prompt/tool echoes cannot qualify, and compare
/// source history before/after this test: viewing cannot prove source immutability.
@MainActor
final class LiveSessionFidelityUITests: XCTestCase {
    private static let environmentPrefix = "LONGHOUSE_FIDELITY_"
    private static let renderTimeout: TimeInterval = 60

    private struct Configuration {
        let serverURL: URL
        let authToken: String
        let sessionID: String
        let markers: [String]
    }

    private struct Observation: Codable {
        let text: String
        let counts: [Int]
        let offsets: [Int?]
        let ordered: Bool
        let finalMarkerIntersectsViewport: Bool
        let finalMarkerFrame: CGRect?
    }

    private struct PhaseEvidence: Codable {
        let phase: String
        let sessionID: String
        var operatingSystem = ProcessInfo.processInfo.operatingSystemVersionString
        let serverURL: String
        let markers: [String]
        var elapsedMS: Double = 0
        var webViewAvailableMS: Double?
        var accessibilityWebViewCount: Int?
        var firstMarkerInViewportMS: Double?
        var allMarkersAccessibleMS: Double?
        var observation: Observation?
        var paintedFinalText: String?
        var paintedFinalMarkerCount: Int?
        var status = "fail"
    }

    private struct ProofFailure: Error, CustomStringConvertible {
        let description: String
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testRealSessionColdOpenAndReopen() async throws {
        #if !targetEnvironment(simulator)
        throw ProofFailure(description: "Live session fidelity is simulator-only; no physical device is supported")
        #else
        let configuration = try configuration()
        try await requireHiddenSession(configuration)
        let app = XCUIApplication()
        // Never inherit fixture/reset hooks from a different UI proof. Resetting
        // on this launch bypasses the application's real headless sign-in path.
        app.launchEnvironment = [
            "LONGHOUSE_HEADLESS_SERVER_URL": configuration.serverURL.absoluteString,
            "LONGHOUSE_HEADLESS_AUTH_TOKEN": configuration.authToken,
            "LONGHOUSE_HEADLESS_OPEN_SESSION": configuration.sessionID,
        ]
        app.terminate()
        defer { app.terminate() }
        // Cold means process-cold, not an erased simulator or cleared disk cache.
        try observeLaunch(app, configuration: configuration, phase: "cold-open")
        app.terminate()
        try observeLaunch(app, configuration: configuration, phase: "terminate-reopen")
        #endif
    }

    private func configuration() throws -> Configuration {
        let environment = ProcessInfo.processInfo.environment
        let keys = ["SERVER_URL", "AUTH_TOKEN", "SESSION_ID", "MARKERS_JSON"]
        if keys.allSatisfy({ environment[Self.environmentPrefix + $0] == nil }) {
            // Normal LonghouseSmoke runs have no live session. The focused runner
            // rejects missing inputs before xcodebuild, so a requested proof
            // cannot silently turn into a skipped test.
            throw XCTSkip("Live-session proof not requested: LONGHOUSE_FIDELITY_* inputs are absent")
        }
        func required(_ suffix: String) throws -> String {
            guard let value = environment[Self.environmentPrefix + suffix],
                  !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw ProofFailure(description: "Missing LONGHOUSE_FIDELITY_\(suffix)")
            }
            return value
        }
        let rawURL = try required("SERVER_URL")
        guard let serverURL = URL(string: rawURL),
              ["http", "https"].contains(serverURL.scheme?.lowercased() ?? ""),
              serverURL.host != nil, serverURL.user == nil, serverURL.password == nil,
              serverURL.query == nil, serverURL.fragment == nil else {
            throw ProofFailure(description: "SERVER_URL must be an HTTP(S) Runtime Host URL without embedded credentials")
        }
        let authToken = try required("AUTH_TOKEN")
        let sessionID = try required("SESSION_ID")
        guard UUID(uuidString: sessionID) != nil else {
            throw ProofFailure(description: "SESSION_ID must be a Longhouse session UUID")
        }
        let markersJSON = try required("MARKERS_JSON")
        guard let markers = try? JSONDecoder().decode([String].self, from: Data(markersJSON.utf8)),
              !markers.isEmpty,
              markers.allSatisfy({ !$0.isEmpty && $0.rangeOfCharacter(from: .whitespacesAndNewlines) == nil
                  && $0.contains(where: { $0.isLetter || $0.isNumber }) }),
              Set(markers).count == markers.count else {
            throw ProofFailure(description: "MARKERS_JSON must be a nonempty array of distinct, whitespace-free final reply markers")
        }
        for (index, marker) in markers.enumerated() {
            guard !markers.enumerated().contains(where: { $0.offset != index && $0.element.contains(marker) }) else {
                throw ProofFailure(description: "Final reply markers must not contain one another")
            }
        }
        return Configuration(serverURL: serverURL, authToken: authToken, sessionID: sessionID, markers: markers)
    }

    private func requireHiddenSession(_ configuration: Configuration) async throws {
        // Qualify session safety and assistant authorship, never client rendering.
        let url = configuration.serverURL.appendingPathComponent("api/agents/sessions/\(configuration.sessionID)/workspace")
        var request = URLRequest(url: url)
        request.setValue(configuration.authToken, forHTTPHeaderField: "X-Agents-Token")
        request.timeoutInterval = 30
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ProofFailure(description: "Read-only hidden-session preflight could not reach the selected Runtime Host")
        }
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let workspace = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let catalog = workspace["session"] as? [String: Any],
              (catalog["id"] as? String)?.lowercased() == configuration.sessionID.lowercased(),
              (catalog["hidden_from_default_timeline"] as? Bool == true || catalog["launch_surface"] as? String == "test") else {
            throw ProofFailure(description: "Selected session must exist on the Runtime Host and be hidden_from_default_timeline")
        }
        guard let projection = workspace["projection"] as? [String: Any],
              projection["has_more"] as? Bool == false,
              projection["page_offset"] as? Int == 0,
              let items = projection["items"] as? [[String: Any]] else {
            throw ProofFailure(description: "Preflight requires a complete projection page to exclude missing or duplicate replies")
        }
        var replies: [String] = []
        for item in items {
            guard item["kind"] as? String == "event",
                  (item["session_id"] as? String)?.lowercased() == configuration.sessionID.lowercased(),
                  let event = item["event"] as? [String: Any] else { continue }
            let text = (event["content_text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let toolText = event["tool_output_text"] as? String ?? ""
            // Include extended/duplicate matches rather than filtering them away.
            // User and tool echoes must not stand in for a missing final reply.
            guard configuration.markers.contains(where: { text.contains($0) || toolText.contains($0) }) else { continue }
            guard event["role"] as? String == "assistant",
                  event["event_origin"] as? String == "durable",
                  (event["tool_name"] as? String ?? "").isEmpty,
                  let id = event["id"] as? String, !id.isEmpty,
                  !configuration.markers.contains(where: toolText.contains),
                  configuration.markers.contains(text) else {
                throw ProofFailure(description: "Preflight marker must be an exact durable assistant textual reply without a prompt or tool echo")
            }
            replies.append(text)
        }
        guard replies == configuration.markers else {
            throw ProofFailure(description: "Preflight final assistant replies must occur exactly once and in expected order")
        }
    }

    private func observeLaunch(_ app: XCUIApplication, configuration: Configuration, phase: String) throws {
        let started = ProcessInfo.processInfo.systemUptime
        var evidence = PhaseEvidence(
            phase: phase,
            sessionID: configuration.sessionID,
            serverURL: configuration.serverURL.absoluteString,
            markers: configuration.markers
        )
        var evidenceScreenshot: XCUIScreenshot?
        defer {
            evidence.elapsedMS = elapsedMS(since: started)
            let screenshot = XCTAttachment(screenshot: evidenceScreenshot ?? app.screenshot())
            screenshot.name = "fidelity-\(phase)-\(evidence.status)"
            screenshot.lifetime = .keepAlways
            add(screenshot)
            // Do not attach launchEnvironment, requests, or runner environment:
            // they carry credentials. Redact even accidental transcript echoes.
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            if let data = try? encoder.encode(evidence), let json = String(data: data, encoding: .utf8) {
                let attachment = XCTAttachment(data: Data(json.replacingOccurrences(of: configuration.authToken, with: "[REDACTED]").utf8), uniformTypeIdentifier: "public.json")
                attachment.name = "fidelity-\(phase)-metrics.json"
                attachment.lifetime = .keepAlways
                add(attachment)
            }
        }
        app.launch()
        let transcript = app.descendants(matching: .any)["session-chat-transcript"]
        guard transcript.waitForExistence(timeout: Self.renderTimeout),
              app.webViews.firstMatch.waitForExistence(timeout: max(0, Self.renderTimeout - elapsedMS(since: started) / 1_000)) else {
            throw ProofFailure(description: "\(phase): real WKWebView transcript must be available")
        }
        evidence.webViewAvailableMS = elapsedMS(since: started)
        // XCTest may expose nested WebView accessibility nodes for one WKWebView.
        // Qualify the transcript's text, not an incidental accessibility-node count.
        evidence.accessibilityWebViewCount = app.webViews.count
        let webView = app.webViews.firstMatch
        var stableSince: TimeInterval?
        let deadline = started + Self.renderTimeout
        repeat {
            // A single snapshot preserves tree order and avoids stitching
            // together text from different renders. Only leaf static text is
            // counted; aggregate parent labels would double count descendants.
            let snapshot = try webView.snapshot()
            let leaves = staticTextLeaves(snapshot)
            let viewport = webView.frame.intersection(app.frame)
            let chrome = app.descendants(matching: .any)["session-chat-bottom-chrome-card"]
            let visibleBottom = chrome.exists ? min(viewport.maxY, chrome.frame.minY) : viewport.maxY
            let visibleViewport = CGRect(x: viewport.minX, y: viewport.minY, width: viewport.width, height: max(0, visibleBottom - viewport.minY))
            let observation = observe(leaves, markers: configuration.markers, viewport: visibleViewport)
            evidence.observation = observation
            if evidence.firstMarkerInViewportMS == nil,
               leaves.contains(where: { leaf in
                   configuration.markers.contains(where: leaf.label.contains) && intersectsViewport(leaf.frame, visibleViewport)
               }) {
                evidence.firstMarkerInViewportMS = elapsedMS(since: started)
            }
            guard !observation.counts.contains(where: { $0 > 1 }) else {
                throw ProofFailure(description: "\(phase): duplicate final reply marker in WKWebView accessibility text")
            }
            guard observation.ordered else {
                throw ProofFailure(description: "\(phase): final reply markers are out of order in WKWebView accessibility text")
            }
            if observation.counts.allSatisfy({ $0 == 1 }) && observation.finalMarkerIntersectsViewport {
                if evidence.allMarkersAccessibleMS == nil {
                    evidence.allMarkersAccessibleMS = elapsedMS(since: started)
                }
                let now = ProcessInfo.processInfo.systemUptime
                if let stableSince, now - stableSince >= 1 {
                    let screenshot = app.screenshot()
                    evidenceScreenshot = screenshot
                    guard let frame = observation.finalMarkerFrame else {
                        throw ProofFailure(description: "\(phase): final marker has no screen geometry")
                    }
                    let finalMarker = configuration.markers[configuration.markers.count - 1]
                    let painted = try paintedText(screenshot, frame: frame, appFrame: app.frame, marker: finalMarker)
                    evidence.paintedFinalText = painted
                    let normalized = painted.filter { $0.isLetter || $0.isNumber }.lowercased()
                    let needle = finalMarker.filter { $0.isLetter || $0.isNumber }.lowercased()
                    let count = normalized.components(separatedBy: needle).count - 1
                    evidence.paintedFinalMarkerCount = count
                    guard count == 1 else {
                        throw ProofFailure(description: "\(phase): final reply exists in accessibility but is not readable exactly once in screenshot pixels")
                    }
                    evidence.status = "pass"
                    return
                }
                stableSince = stableSince ?? now
            } else {
                stableSince = nil
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        } while ProcessInfo.processInfo.systemUptime < deadline
        throw ProofFailure(description: "\(phase): markers missing or final marker outside transcript viewport; unavailable WebKit accessibility is a failure, not a render-callback fallback")
    }

    private func staticTextLeaves(_ node: XCUIElementSnapshot) -> [XCUIElementSnapshot] {
        let descendants = node.children.flatMap { staticTextLeaves($0) }
        if node.elementType == .staticText && descendants.isEmpty {
            return node.label.isEmpty ? [] : [node]
        }
        return descendants
    }

    private func observe(_ leaves: [XCUIElementSnapshot], markers: [String], viewport: CGRect) -> Observation {
        // A separator prevents inventing a contiguous marker across text nodes.
        let text = leaves.map(\.label).joined(separator: "\n")
        let source = text as NSString
        var counts: [Int] = []
        var offsets: [Int?] = []
        for marker in markers {
            var search = NSRange(location: 0, length: source.length)
            var count = 0
            var firstOffset: Int?
            while search.length > 0 {
                let range = source.range(of: marker, options: .literal, range: search)
                if range.location == NSNotFound { break }
                count += 1
                firstOffset = firstOffset ?? range.location
                // Count overlapping occurrences too; never deduplicate labels.
                search = NSRange(location: range.location + 1, length: source.length - range.location - 1)
            }
            counts.append(count)
            offsets.append(firstOffset)
        }
        let present = offsets.compactMap { $0 }
        let ordered = zip(present, present.dropFirst()).allSatisfy { $0 < $1 }
        let finalMarker = markers[markers.count - 1]
        let finalLeaf = leaves.first { $0.label.contains(finalMarker) && intersectsViewport($0.frame, viewport) }
        return Observation(text: text, counts: counts, offsets: offsets, ordered: ordered,
                           finalMarkerIntersectsViewport: finalLeaf != nil,
                           finalMarkerFrame: finalLeaf?.frame.intersection(viewport))
    }

    private func intersectsViewport(_ frame: CGRect, _ viewport: CGRect) -> Bool {
        let intersection = frame.intersection(viewport)
        return !intersection.isNull && intersection.width > 1 && intersection.height > 1
    }

    private func paintedText(_ screenshot: XCUIScreenshot, frame: CGRect, appFrame: CGRect, marker: String) throws -> String {
        guard let image = screenshot.image.cgImage, appFrame.width > 0, appFrame.height > 0 else {
            throw ProofFailure(description: "Screenshot pixels are unavailable")
        }
        let scaleX = CGFloat(image.width) / appFrame.width
        let scaleY = CGFloat(image.height) / appFrame.height
        let crop = CGRect(x: (frame.minX - appFrame.minX) * scaleX,
                          y: (frame.minY - appFrame.minY) * scaleY,
                          width: frame.width * scaleX, height: frame.height * scaleY).integral
        guard let pixels = image.cropping(to: crop) else {
            throw ProofFailure(description: "Final reply has no screenshot pixel region")
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = true
        request.customWords = [marker]
        try VNImageRequestHandler(cgImage: pixels, options: [:]).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    }

    private func elapsedMS(since started: TimeInterval) -> Double {
        (ProcessInfo.processInfo.systemUptime - started) * 1_000
    }
}
