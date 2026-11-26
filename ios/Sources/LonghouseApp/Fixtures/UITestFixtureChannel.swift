#if DEBUG
import SwiftUI
import UIKit

/// Switches the running app to another UI-test fixture without relaunching it.
///
/// A launch costs a UI test 5 to 8 s on the bench simulator (terminate, automation
/// session, first idle), about two thirds of the UI suite when every test launched.
/// A test that launched with LONGHOUSE_UI_TEST_FIXTURE_CHANNEL writes `command.json`
/// into that directory instead: the complete LONGHOUSE_UI_TEST_ environment of the next
/// fixture, its appearance and text size. The app closes whatever the last fixture left
/// open, applies the command as if it had been launched with it, rebuilds the root view
/// (a new session view, view model and stores, as on launch) and publishes the command's
/// token in `ui-test-fixture-ready` once the new view is in place.
@MainActor
final class UITestFixtureChannel: ObservableObject {
    static let shared = UITestFixtureChannel()

    /// Changes with each applied command; the root view takes it as its identity.
    @Published private(set) var generation = 0
    /// "launch" until the first command: the marker shows from launch, so a test can tell
    /// an app it can command from one launched without the channel.
    @Published private(set) var token = "launch"

    private struct Command: Decodable {
        let token: String
        let environment: [String: String]
        let appearance: String?
        let contentSizeCategory: String?
    }

    /// True between taking the last fixture down and building the next.
    @Published private(set) var isSwitching = false

    private var source: DispatchSourceFileSystemObject?
    private var pendingCommand: Command?
    private var appliedToken: String?

    func startIfRequested() {
        guard source == nil, let directory = UITestHooks.fixtureChannelDirectory else { return }
        try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        let descriptor = open(directory, O_EVTONLY)
        guard descriptor >= 0 else { return }
        // The test writes the command atomically (a rename into the directory), which is
        // a write event on the directory itself.
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor,
            eventMask: .write,
            queue: .main
        )
        source.setEventHandler { [weak self] in
            MainActor.assumeIsolated { self?.readCommand(in: directory) }
        }
        source.setCancelHandler { close(descriptor) }
        source.resume()
        self.source = source
        readCommand(in: directory)
    }

    private func readCommand(in directory: String) {
        let url = URL(fileURLWithPath: directory).appendingPathComponent("command.json")
        guard let data = try? Data(contentsOf: url),
              let command = try? JSONDecoder().decode(Command.self, from: data),
              command.token != appliedToken else { return }
        appliedToken = command.token
        apply(command)
    }

    private func apply(_ command: Command) {
        let windows = Self.windows
        for window in windows {
            // Close the keyboard, a sheet or a menu the last fixture left open, at once.
            window.endEditing(true)
            window.rootViewController?.dismiss(animated: false)
        }
        // First take the last fixture down to an empty root, so its views are gone before the
        // next one is built (finishSwitch, from that empty root's onAppear).
        pendingCommand = command
        isSwitching = true
        generation += 1
    }

    /// Called once the empty root is on screen: the last fixture's views are torn down.
    func finishSwitch() {
        guard let command = pendingCommand else { return }
        pendingCommand = nil
        // Its transcript web view went back to the pool still holding its document, which a
        // launch would not have; the next fixture starts with a new one, as on launch.
        WebTranscriptWebViewPool.discardWarmSpare()
        UITestFixtureOverrides.shared.replace(environment: command.environment, appearance: command.appearance)
        let style: UIUserInterfaceStyle = switch UITestHooks.appearanceOverride {
        case "light": .light
        case "dark": .dark
        default: .unspecified
        }
        for window in Self.windows {
            window.overrideUserInterfaceStyle = style
            if let category = command.contentSizeCategory {
                window.traitOverrides.preferredContentSizeCategory = UIContentSizeCategory(rawValue: category)
            } else if window.traitOverrides.contains(UITraitPreferredContentSizeCategory.self) {
                window.traitOverrides.remove(UITraitPreferredContentSizeCategory.self)
            }
        }
        token = command.token
        isSwitching = false
        generation += 1
    }

    private static var windows: [UIWindow] {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap(\.windows)
    }
}

/// The token of the last applied fixture command, drawn inside the rebuilt root view so a
/// test sees it only once that view exists.
struct UITestFixtureReadyMarker: View {
    let token: String

    var body: some View {
        Text(token)
            .font(.system(size: 1))
            .frame(width: 2, height: 2)
            .clipped()
            .accessibilityIdentifier("ui-test-fixture-ready")
            .allowsHitTesting(false)
    }
}
#endif
