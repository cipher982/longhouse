import SwiftUI

#Preview("Machine chooser · dark") {
    NavigationStack {
        MachineSelectionView(
            machines: MachinePreviewFixtures.directoryMachines,
            selectedDeviceId: "cinder",
            onSelect: { _ in }
        )
    }
    .preferredColorScheme(.dark)
    .emberChrome()
}

#Preview("Machine chooser · light") {
    NavigationStack {
        MachineSelectionView(
            machines: MachinePreviewFixtures.directoryMachines,
            selectedDeviceId: "cinder",
            onSelect: { _ in }
        )
    }
    .preferredColorScheme(.light)
    .emberChrome()
}
