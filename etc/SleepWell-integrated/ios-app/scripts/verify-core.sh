#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if xcrun --sdk macosx --show-sdk-platform-path >/dev/null 2>&1; then
    exec swift test
fi
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/sleepwell-core.XXXXXX")
swiftc -emit-library -emit-module -enable-testing -module-name SleepWellCore \
    Sources/SleepWellCore/*.swift -emit-module-path "$build_dir/SleepWellCore.swiftmodule" \
    -o "$build_dir/libSleepWellCore.dylib"
swiftc -I "$build_dir" -L "$build_dir" -lSleepWellCore \
    -Xlinker -rpath -Xlinker "$build_dir" \
    Tests/SleepWellCoreTests/CoreTests.swift CLITests/main.swift -o "$build_dir/check"
"$build_dir/check"
printf 'Verification build: %s\n' "$build_dir"
