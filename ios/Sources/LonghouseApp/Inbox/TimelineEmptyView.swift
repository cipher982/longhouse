import SwiftUI

/// The timeline of a Runtime Host that has no sessions yet. A brand-new host
/// has no machine either, and the phone cannot connect one, so the screen says
/// where to do it instead of promising a sync that has nothing to sync from.
struct TimelineEmptyView: View {
    var body: some View {
        ContentUnavailableView(
            "No sessions yet",
            systemImage: "rectangle.stack",
            description: Text(
                "New sessions you start from now on appear here as Longhouse syncs them from your machines. "
                    + "Existing history stays on the computer unless you choose a project, date, or all history there during setup or later. "
                    + "No machine yet? On your computer, open your Longhouse in a browser and run the command under \u{201C}Connect your first machine\u{201D}."
            )
        )
        .accessibilityIdentifier("timeline-empty")
    }
}
