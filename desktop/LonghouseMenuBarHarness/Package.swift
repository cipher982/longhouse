// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "LonghouseMenuBarHarness",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(name: "LonghouseMenuBarCore", targets: ["LonghouseMenuBarCore"]),
        .executable(name: "LonghouseMenuBarHarnessSnapshot", targets: ["LonghouseMenuBarHarnessSnapshot"]),
        .executable(name: "LonghouseMenuBarHarnessApp", targets: ["LonghouseMenuBarHarnessApp"]),
        .executable(name: "LonghouseMenuBarHarnessMenuBar", targets: ["LonghouseMenuBarHarnessMenuBar"]),
    ],
    targets: [
        .target(
            name: "LonghouseMenuBarCore"
        ),
        .executableTarget(
            name: "LonghouseMenuBarHarnessSnapshot",
            dependencies: ["LonghouseMenuBarCore"]
        ),
        .executableTarget(
            name: "LonghouseMenuBarHarnessApp",
            dependencies: ["LonghouseMenuBarCore"]
        ),
        .executableTarget(
            name: "LonghouseMenuBarHarnessMenuBar",
            dependencies: ["LonghouseMenuBarCore"]
        ),
        .testTarget(
            name: "LonghouseMenuBarCoreTests",
            dependencies: ["LonghouseMenuBarCore"]
        ),
    ]
)
