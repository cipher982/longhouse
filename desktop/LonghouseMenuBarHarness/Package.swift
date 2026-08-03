// swift-tools-version: 6.1

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
            name: "LonghouseMenuBarCore",
            resources: [
                .process("Resources"),
            ]
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
            dependencies: ["LonghouseMenuBarCore"],
            // Read via #filePath, not Bundle. Declaring it as a resource would
            // synthesise a Bundle.module for this target and shadow the core
            // target's bundle that other tests rely on.
            exclude: ["Fixtures/native-desktop-health.json"]
        ),
    ]
)
