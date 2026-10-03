import SwiftUI

#if DEBUG
/// Deterministic simulator entry point for the Machines surfaces. It is only
/// selected by the explicit LONGHOUSE_UI_TEST_MACHINES_FIXTURE environment
/// variable and all data is local to the app.
struct MachinesUITestFixtureView: View {
    var body: some View {
        Group {
            switch UITestHooks.machineFixtureScreen {
            case "detail":
                NavigationStack {
                    MachineDetailView(summary: MachinePreviewFixtures.cinder)
                }
            case "chooser":
                NavigationStack {
                    MachineSelectionView(
                        machines: MachinePreviewFixtures.directoryMachines,
                        selectedDeviceId: "cinder",
                        onSelect: { _ in }
                    )
                }
            default:
                NavigationStack {
                    MachinesView(previewResponse: MachinePreviewFixtures.response)
                }
            }
        }
        .preferredColorScheme(UITestHooks.appearanceOverride == "light" ? .light : .dark)
        .emberChrome()
    }
}
#endif
