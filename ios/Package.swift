// swift-tools-version: 6.1

import PackageDescription

let package = Package(
    name: "LonghouseIOS",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "LonghouseShared", targets: ["LonghouseShared"]),
    ],
    dependencies: [
        .package(url: "https://github.com/google/GoogleSignIn-iOS", from: "8.0.0"),
    ],
    targets: [
        .target(
            name: "LonghouseShared",
            path: "Sources/Shared"
        ),
    ]
)
