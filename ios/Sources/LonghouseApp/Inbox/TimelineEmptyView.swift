import SwiftUI

/// The timeline empty state is about the selected window, not lifetime
/// history. A machine-scoped timeline uses the same honest wording while
/// retaining its directory identity and launch actions.
struct TimelineEmptyView: View {
    let scopedToMachine: Bool

    init(scopedToMachine: Bool = false) {
        self.scopedToMachine = scopedToMachine
    }

    var body: some View {
        ContentUnavailableView(
            "No sessions in this window",
            systemImage: "rectangle.stack",
            description: Text(
                scopedToMachine
                    ? "No sessions appeared for this machine in the selected window."
                    : "No sessions appeared in the selected window. New sessions you start from now on appear here as Longhouse syncs them from your machines."
            )
        )
        .accessibilityIdentifier("timeline-empty")
    }
}
