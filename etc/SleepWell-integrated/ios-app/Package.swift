// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SleepWellCore",
    platforms: [.macOS(.v13), .iOS(.v16)],
    products: [.library(name: "SleepWellCore", targets: ["SleepWellCore"])],
    targets: [
        .target(name: "SleepWellCore"),
        .testTarget(name: "SleepWellCoreTests", dependencies: ["SleepWellCore"])
    ]
)
