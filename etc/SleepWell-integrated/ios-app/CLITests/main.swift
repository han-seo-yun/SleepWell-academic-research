// Run the same assertions when Apple's Command Line Tools lack XCTest.
import Foundation

#if !canImport(XCTest)
class XCTestCase {}
func XCTAssertEqual<T: Equatable>(_ a: T, _ b: T, file: StaticString = #filePath, line: UInt = #line) {
    precondition(a == b, "Expected \(a) == \(b)", file: file, line: line)
}
func XCTAssertEqual(_ a: Double, _ b: Double, accuracy: Double, file: StaticString = #filePath, line: UInt = #line) {
    precondition(abs(a - b) <= accuracy, "Expected \(a) ≈ \(b)", file: file, line: line)
}
func XCTAssertNil<T>(_ a: T?, file: StaticString = #filePath, line: UInt = #line) { precondition(a == nil, file: file, line: line) }
func XCTAssertNotNil<T>(_ a: T?, file: StaticString = #filePath, line: UInt = #line) { precondition(a != nil, file: file, line: line) }
func XCTAssertTrue(_ a: Bool, file: StaticString = #filePath, line: UInt = #line) { precondition(a, file: file, line: line) }
func XCTAssertGreaterThanOrEqual<T: Comparable>(_ a: T, _ b: T, file: StaticString = #filePath, line: UInt = #line) { precondition(a >= b, file: file, line: line) }
func XCTAssertThrowsError<T>(_ expression: @autoclosure () throws -> T, file: StaticString = #filePath, line: UInt = #line) {
    do { _ = try expression() } catch { return }
    preconditionFailure("Expected error", file: file, line: line)
}
func XCTUnwrap<T>(_ value: T?) throws -> T {
    guard let value else { throw NSError(domain: "Unwrap", code: 1) }; return value
}
#endif

let tests = CoreTests()
tests.testPhysicsAnalyticalValues()
tests.testOverlapsDoNotDoubleCountOrInventN2()
try tests.testFutureDataCannotAlterDecision()
try tests.testMissingSensorsStayMissingAndUntrainedFallbackIsExplicit()
try tests.testRidgeJSONEquationAndMalformedModels()
try tests.testMidnightClockMedianAndInvalidTarget()
try tests.testExportCarriesSyntheticFlagAndContract()
print("PASS: 7 core tests (physics, overlap/stages, leakage, missing data, Ridge JSON, midnight, export)")
