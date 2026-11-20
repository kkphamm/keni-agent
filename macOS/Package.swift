// swift-tools-version: 6.1
import PackageDescription

let package = Package(
    name: "LocalVoiceAgent",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/livekit/client-sdk-swift.git", exact: "2.19.0"),
    ],
    targets: [
        .executableTarget(
            name: "LocalVoiceAgent",
            dependencies: [.product(name: "LiveKit", package: "client-sdk-swift")],
            path: "Sources"
        ),
    ]
)
