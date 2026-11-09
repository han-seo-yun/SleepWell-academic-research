import Foundation

public enum SleepStage: String, Codable, CaseIterable {
    case awake, core, deep, rem, asleep, inBed
    public var isSleep: Bool { [.core, .deep, .rem, .asleep].contains(self) }
    // Apple Core does not distinguish N1 and N2. Preserve that missing label.
    public var researchCode: Int {
        switch self { case .awake: return 0; case .deep: return 3; case .rem: return 4; default: return -1 }
    }
    public var label: String {
        switch self {
        case .awake: return "각성"; case .core: return "Core"; case .deep: return "깊은 수면"
        case .rem: return "REM"; case .asleep: return "수면·단계 미상"; case .inBed: return "침대에 있음"
        }
    }
    var priority: Int {
        switch self { case .awake: return 6; case .deep: return 5; case .rem: return 4
        case .core: return 3; case .asleep: return 2; case .inBed: return 1 }
    }
}

public struct SleepSegment: Codable, Equatable {
    public var start: Date
    public var end: Date
    public var stage: SleepStage
    public var source: String
    public init(start: Date, end: Date, stage: SleepStage, source: String = "") {
        self.start = start; self.end = end; self.stage = stage; self.source = source
    }
}

public struct HeartSample: Codable {
    public var time: Date
    public var bpm: Double
    public var source: String
    public init(time: Date, bpm: Double, source: String = "") {
        self.time = time; self.bpm = bpm; self.source = source
    }
}

public struct StepSample: Codable {
    public var start: Date
    public var end: Date
    public var count: Double
    public var source: String
    public init(start: Date, end: Date, count: Double, source: String = "") {
        self.start = start; self.end = end; self.count = count; self.source = source
    }
}

public struct HealthSnapshot: Codable {
    public var source: String
    public var start: Date
    public var end: Date
    public var sleep: [SleepSegment]
    public var heart: [HeartSample]
    public var steps: [StepSample]
    public var warnings: [String]
    public init(source: String, start: Date, end: Date, sleep: [SleepSegment] = [],
                heart: [HeartSample] = [], steps: [StepSample] = [], warnings: [String] = []) {
        self.source = source; self.start = start; self.end = end; self.sleep = sleep
        self.heart = heart; self.steps = steps; self.warnings = warnings
    }

    /// Resolve overlapping in-bed/unspecified/staged records into disjoint intervals.
    public func normalizedSleep(before cutoff: Date? = nil) -> [SleepSegment] {
        let limit = min(cutoff ?? end, end)
        let valid = sleep.filter { $0.start < limit && $0.end > start && $0.end > $0.start }
        let bounds = Set(valid.flatMap { [max(start, $0.start), min(limit, $0.end)] }).sorted()
        guard bounds.count > 1 else { return [] }
        var result: [SleepSegment] = []
        for i in 0..<(bounds.count - 1) {
            let a = bounds[i], b = bounds[i + 1]
            guard let selected = valid.filter({ $0.start < b && $0.end > a })
                .max(by: { $0.stage.priority < $1.stage.priority }) else { continue }
            if let last = result.last, last.end == a, last.stage == selected.stage, last.source == selected.source {
                result[result.count - 1].end = b
            } else { result.append(.init(start: a, end: b, stage: selected.stage, source: selected.source)) }
        }
        return result
    }

    public static func demo(at end: Date = Date(), calendar: Calendar = .current) -> HealthSnapshot {
        let start = calendar.date(byAdding: .day, value: -7, to: calendar.startOfDay(for: end))!
        var sleep: [SleepSegment] = []
        for day in 0..<8 {
            let midnight = calendar.date(byAdding: .day, value: day, to: start)!
            let bed = midnight.addingTimeInterval(30 * 60)
            for i in 0..<14 {
                let a = bed.addingTimeInterval(Double(i) * 1800)
                let b = a.addingTimeInterval(1800)
                if b <= end {
                    let stage: SleepStage = i == 6 ? .awake : (i % 5 == 1 ? .deep : (i % 5 == 4 ? .rem : .core))
                    sleep.append(.init(start: a, end: b, stage: stage, source: "DEMO"))
                }
            }
        }
        var heart: [HeartSample] = []; var steps: [StepSample] = []
        var time = start
        while time < end {
            let h = calendar.component(.hour, from: time)
            heart.append(.init(time: time, bpm: 66 + 9 * sin(time.timeIntervalSince1970 / 18000), source: "DEMO"))
            if time.addingTimeInterval(600) <= end {
                steps.append(.init(start: time, end: time.addingTimeInterval(600), count: (8..<21).contains(h) ? 65 : 0, source: "DEMO"))
            }
            time = time.addingTimeInterval(600)
        }
        return .init(source: "demo", start: start, end: end, sleep: sleep, heart: heart, steps: steps,
                     warnings: ["샘플 데이터입니다. 실제 측정·학습 데이터로 사용하지 마세요."])
    }
}

public struct SleepEpisode {
    public var start: Date
    public var end: Date
    public var sleepMinutes: Double
}

public enum HealthMath {
    public static func median(_ values: [Double]) -> Double? {
        let s = values.filter(\.isFinite).sorted()
        guard !s.isEmpty else { return nil }
        return s.count % 2 == 1 ? s[s.count / 2] : (s[s.count / 2 - 1] + s[s.count / 2]) / 2
    }
    public static func clockMinutes(_ date: Date, calendar: Calendar) -> Double {
        let c = calendar.dateComponents([.hour, .minute, .second], from: date)
        return Double(c.hour! * 60 + c.minute!) + Double(c.second!) / 60
    }
    public static func episodes(_ segments: [SleepSegment]) -> [SleepEpisode] {
        var result: [SleepEpisode] = []
        for s in segments.filter({ $0.stage.isSleep }).sorted(by: { $0.start < $1.start }) {
            let minutes = s.end.timeIntervalSince(s.start) / 60
            if let last = result.last, s.start.timeIntervalSince(last.end) <= 3600 {
                result[result.count - 1].end = s.end
                result[result.count - 1].sleepMinutes += minutes
            } else { result.append(.init(start: s.start, end: s.end, sleepMinutes: minutes)) }
        }
        return result
    }
}
