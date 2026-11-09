#if canImport(XCTest)
import XCTest
#endif
import Foundation
@testable import SleepWellCore

final class CoreTests: XCTestCase {
    let iso = ISO8601DateFormatter()
    var calendar: Calendar {
        var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "Asia/Seoul")!; return c
    }
    func date(_ value: String) -> Date { iso.date(from: value)! }
    var decision: Date { date("2026-10-08T20:00:00+09:00") }

    func testPhysicsAnalyticalValues() {
        XCTAssertEqual(TwoProcess.advance(0.5, sleeping: false, hours: 18.2), 1 - 0.5 / exp(1), accuracy: 1e-12)
        XCTAssertEqual(TwoProcess.advance(0.5, sleeping: true, hours: 4.2), 0.5 / exp(1), accuracy: 1e-12)
        XCTAssertEqual(TwoProcess.circadian(at: date("2026-10-08T03:30:00+09:00"), phase: 3.5, calendar: calendar), 0.15, accuracy: 1e-12)
    }
    func testOverlapsDoNotDoubleCountOrInventN2() {
        let start = date("2026-10-08T00:00:00+09:00"), end = start.addingTimeInterval(3600)
        let s = HealthSnapshot(source: "test", start: start, end: end, sleep: [
            .init(start: start, end: end, stage: .inBed), .init(start: start, end: end, stage: .asleep),
            .init(start: start, end: start.addingTimeInterval(1800), stage: .core),
            .init(start: start.addingTimeInterval(1800), end: end, stage: .deep)])
        let normalized = s.normalizedSleep()
        XCTAssertEqual(normalized.count, 2)
        XCTAssertEqual(HealthMath.episodes(normalized).first?.sleepMinutes, 60)
        XCTAssertEqual(normalized[0].stage.researchCode, -1)
        XCTAssertEqual(normalized[1].stage.researchCode, 3)
        let csv = ResearchExport.epochCSV(s, calendar: calendar)
        XCTAssertEqual(csv.split(separator: "\n").count, 121)
        XCTAssertTrue(csv.contains("\"20261007\",\"apple-20261007\",\"1440\",\"-1\""))
    }
    func testFutureDataCannotAlterDecision() throws {
        let s = HealthSnapshot.demo(at: decision, calendar: calendar)
        let a = try TwoProcess.analyze(s, decision: decision, calendar: calendar)
        var contaminated = s
        contaminated.end = decision.addingTimeInterval(86400)
        contaminated.heart += [.init(time: decision, bpm: 999), .init(time: decision.addingTimeInterval(600), bpm: 999)]
        contaminated.steps += [.init(start: decision.addingTimeInterval(-60), end: decision.addingTimeInterval(60), count: 99999)]
        contaminated.sleep += [.init(start: decision.addingTimeInterval(3600), end: decision.addingTimeInterval(36000), stage: .deep)]
        let b = try TwoProcess.analyze(contaminated, decision: decision, calendar: calendar)
        XCTAssertEqual(a.features, b.features)
        XCTAssertEqual(a.predictedBed, b.predictedBed)
        XCTAssertEqual(a.history.last?.s, b.history.last?.s)
        XCTAssertEqual(a.history.last?.time, decision)
    }
    func testMissingSensorsStayMissingAndUntrainedFallbackIsExplicit() throws {
        var s = HealthSnapshot.demo(at: decision, calendar: calendar); s.steps = []; s.heart = []
        let a = try TwoProcess.analyze(s, decision: decision, calendar: calendar)
        XCTAssertNil(a.features["steps_pre20"])
        XCTAssertNil(a.features["day_hr_mean_pre20"])
        XCTAssertNil(a.modelVersion)
        XCTAssertEqual(a.predictedBed, a.physicalBed)
        XCTAssertGreaterThanOrEqual(a.recommendedBed, decision)
    }
    func testRidgeJSONEquationAndMalformedModels() throws {
        let json = """
        {"schema_version":1,"model_version":"test-only","status":"trained",
         "feature_order":["current_s","caffeine_mg"],
         "preprocessing":{"imputation_median":[0.5,100],"mean":[0.4,80],"scale":[0.1,10]},
         "targets":{"bedtime_residual_min":{"intercept":2,"coefficients":[3,4]},
                    "wake_residual_min":{"intercept":-1,"coefficients":[-2,1]}}}
        """
        let model = try RidgeModel.load(Data(json.utf8))
        let prediction = try model.predict(["current_s": 0.6])
        XCTAssertEqual(prediction.bed, 16, accuracy: 1e-10)
        XCTAssertEqual(prediction.wake, -3, accuracy: 1e-10)
        XCTAssertThrowsError(try RidgeModel.load(Data(json.replacingOccurrences(of: "trained", with: "untrained").utf8)))
        XCTAssertThrowsError(try RidgeModel.load(Data(json.replacingOccurrences(of: "[3,4]", with: "[3]").utf8)))
        XCTAssertThrowsError(try RidgeModel.load(Data(json.replacingOccurrences(of: "caffeine_mg", with: "actual_bedtime_minute").utf8)))
    }
    func testMidnightClockMedianAndInvalidTarget() throws {
        let s = HealthSnapshot.demo(at: decision, calendar: calendar)
        XCTAssertThrowsError(try TwoProcess.analyze(s, decision: decision, calendar: calendar, targetWake: decision))
        let episodes = [SleepEpisode(start: date("2026-10-06T23:50:00+09:00"), end: date("2026-10-07T07:00:00+09:00"), sleepMinutes: 430),
                        SleepEpisode(start: date("2026-10-08T00:10:00+09:00"), end: date("2026-10-08T07:00:00+09:00"), sleepMinutes: 410)]
        let f = FeatureBuilder.build(snapshot: s, current: .init(time: decision, s: 0.5, c: 0), episodes: episodes,
                                     survey: Survey(), targetWake: nil, calendar: calendar)
        XCTAssertEqual(f["prior_bedtime_median_7"], 0)
    }
    func testExportCarriesSyntheticFlagAndContract() throws {
        let s = HealthSnapshot.demo(at: decision, calendar: calendar)
        let a = try TwoProcess.analyze(s, decision: decision, calendar: calendar)
        let files = try ResearchExport.files(snapshot: s, analysis: a, calendar: calendar)
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: files["snapshot_DEMO.json"]!) as? [String: Any])
        XCTAssertEqual(json["is_synthetic"] as? Bool, true)
        XCTAssertEqual(json["feature_contract"] as? String, FeatureBuilder.contract)
        XCTAssertNotNil(files["epoch_30s_DEMO.csv"])
    }
}
