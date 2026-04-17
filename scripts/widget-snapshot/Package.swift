// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "WidgetSnapshot",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "WidgetSnapshot", path: "Sources")
    ]
)
