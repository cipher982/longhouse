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
    targets: [
        .target(
            name: "LonghouseShared",
            path: "Sources/Shared"
        ),
    ]
)
