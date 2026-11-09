import Foundation

public struct ProcessPoint: Identifiable, Codable {
    public var id: Date { time }
    public let time: Date
    public let s: Double
    public let c: Double
    public var propensity: Double { s + c }
}

public struct Survey: Codable {
    public var caffeineMg: Double?
    public var caffeineTime: Date?
    public var napMinutes: Double?
    public var napEnd: Date?
    public var exerciseMinutes: Double?
    public var exerciseIntensity: Double?
    public var fatigue: Double?
    public init(caffeineMg: Double? = nil, caffeineTime: Date? = nil, napMinutes: Double? = nil,
                napEnd: Date? = nil, exerciseMinutes: Double? = nil, exerciseIntensity: Double? = nil, fatigue: Double? = nil) {
        self.caffeineMg = caffeineMg; self.caffeineTime = caffeineTime; self.napMinutes = napMinutes
        self.napEnd = napEnd; self.exerciseMinutes = exerciseMinutes; self.exerciseIntensity = exerciseIntensity; self.fatigue = fatigue
    }
}

public struct Analysis: Codable {
    public let decision: Date
    public let history: [ProcessPoint]
    public let future: [ProcessPoint]
    public let physicalBed: Date
    public let physicalWake: Date
    public let predictedBed: Date
    public let predictedWake: Date
    public let recommendedBed: Date
    public let recommendedWake: Date
    public let features: [String: Double]
    public let modelVersion: String?
    public let warnings: [String]
}

public enum TwoProcess {
    public static func advance(_ s: Double, sleeping: Bool, hours: Double) -> Double {
        sleeping ? s * exp(-hours / 4.2) : 1 - (1 - s) * exp(-hours / 18.2)
    }
    public static func circadian(at date: Date, phase: Double, calendar: Calendar) -> Double {
        0.15 * cos(2 * .pi * (HealthMath.clockMinutes(date, calendar: calendar) / 60 - phase) / 24)
    }
    public static func analyze(_ snapshot: HealthSnapshot, decision: Date, calendar: Calendar = .current,
                               survey: Survey = Survey(), targetWake: Date? = nil,
                               model: RidgeModel? = nil) throws -> Analysis {
        guard decision <= snapshot.end, decision > snapshot.start else {
            throw ModelError.invalid("예측 시각을 포함하는 건강 기록이 필요합니다.")
        }
        if let targetWake, targetWake <= decision {
            throw ModelError.invalid("목표 기상 시각은 예측 시각 이후여야 합니다.")
        }
        let start = max(snapshot.start, decision.addingTimeInterval(-7 * 86400))
        // Exclude unfinished sleep, including records whose final endpoint is in the future.
        var past = snapshot
        past.sleep = snapshot.sleep.filter { $0.end <= decision }
        let segments = past.normalizedSleep(before: decision).filter { $0.end > start }
        let episodes = HealthMath.episodes(segments)
        guard !episodes.isEmpty else { throw ModelError.invalid("완료된 수면 기록이 없습니다. 권한·동기화 상태를 확인하거나 샘플 모드를 사용하세요.") }
        let midpoints = episodes.map { HealthMath.clockMinutes($0.start.addingTimeInterval($0.end.timeIntervalSince($0.start) / 2), calendar: calendar) / 60 }
        let sinSum = midpoints.reduce(0) { $0 + sin($1 * 2 * .pi / 24) }
        let cosSum = midpoints.reduce(0) { $0 + cos($1 * 2 * .pi / 24) }
        let phase = atan2(sinSum, cosSum) * 24 / (2 * .pi)
        var history: [ProcessPoint] = [], s = 0.5, cursor = start
        var segmentIndex = 0
        while cursor < decision {
            history.append(.init(time: cursor, s: s, c: circadian(at: cursor, phase: phase, calendar: calendar)))
            let end = min(cursor.addingTimeInterval(60), decision)
            while segmentIndex < segments.count && segments[segmentIndex].end <= cursor { segmentIndex += 1 }
            let sleeping = segments[segmentIndex...].prefix { $0.start < end }.contains { $0.stage.isSleep && $0.end > cursor }
            s = advance(s, sleeping: sleeping, hours: end.timeIntervalSince(cursor) / 3600)
            cursor = end
        }
        let current = ProcessPoint(time: decision, s: s, c: circadian(at: decision, phase: phase, calendar: calendar))
        history.append(current)
        var future: [ProcessPoint] = []
        for i in 0..<96 {
            let time = decision.addingTimeInterval(Double(i) * 900)
            future.append(.init(time: time, s: s, c: circadian(at: time, phase: phase, calendar: calendar)))
            s = advance(s, sleeping: false, hours: 0.25)
        }
        let night = future.filter { let h = calendar.component(.hour, from: $0.time); return h >= 20 || h < 5 }
        let physicalBed = (night.isEmpty ? future : night).max { $0.propensity < $1.propensity }!.time
        let sleepMinutes = min(600, max(360, HealthMath.median(episodes.map(\.sleepMinutes)) ?? 480))
        let physicalWake = targetWake.flatMap { $0 > physicalBed ? $0 : nil } ?? physicalBed.addingTimeInterval(sleepMinutes * 60)
        let features = FeatureBuilder.build(snapshot: past, current: current, episodes: episodes,
                                            survey: survey, targetWake: targetWake, calendar: calendar)
        var warnings = snapshot.warnings + ["수면 기록 밖의 구간은 각성으로 가정합니다. 미착용·누락 수면은 수면압력을 왜곡할 수 있습니다.",
                                            "미래 곡선은 계속 깨어 있는 경우의 시뮬레이션입니다."]
        var residual = (bed: 0.0, wake: 0.0)
        var usedModel: String?
        if let model {
            do {
                residual = try model.predict(features)
                // Reject pathological outputs rather than display an unusable schedule.
                guard abs(residual.bed) <= 720, abs(residual.wake) <= 720 else {
                    throw ModelError.invalid("잔차가 12시간 범위를 벗어나 물리모델로 계산했습니다.")
                }
                usedModel = model.model_version
            } catch { residual = (0, 0); warnings.append(error.localizedDescription) }
        }
        var predictedBed = physicalBed.addingTimeInterval(residual.bed * 60)
        var predictedWake = physicalWake.addingTimeInterval(residual.wake * 60)
        if predictedBed < decision || predictedWake <= predictedBed {
            predictedBed = physicalBed; predictedWake = physicalWake; usedModel = nil
            warnings.append("학습 모델의 시간 순서가 유효하지 않아 물리모델로 계산했습니다.")
        }
        let wake = targetWake ?? predictedWake
        let bed = max(decision, min(predictedBed, wake.addingTimeInterval(-sleepMinutes * 60)))
        if wake.timeIntervalSince(bed) < sleepMinutes * 60 {
            warnings.append("목표 기상까지 최근 수면시간을 확보하기 어렵습니다.")
        }
        return .init(decision: decision, history: history, future: future,
                     physicalBed: physicalBed, physicalWake: physicalWake, predictedBed: predictedBed,
                     predictedWake: predictedWake, recommendedBed: bed, recommendedWake: wake,
                     features: features, modelVersion: usedModel, warnings: warnings)
    }
}

public enum FeatureBuilder {
    public static let contract = "ios_pre20_v1"
    public static let names = ["current_s", "current_c", "current_propensity", "day_hr_mean_pre20",
        "day_hr_std_pre20", "late_hr_mean_pre20", "steps_pre20", "active_minutes_pre20",
        "previous_sleep_duration_h", "prior_bedtime_median_7", "prior_wake_median_7", "prior_duration_median_7",
        "caffeine_mg", "caffeine_gap_min", "nap_minutes", "nap_end_gap_min", "exercise_minutes",
        "exercise_intensity", "fatigue_score", "target_wake_minute", "weekend"]

    public static func build(snapshot: HealthSnapshot, current: ProcessPoint, episodes: [SleepEpisode],
                             survey: Survey, targetWake: Date?, calendar: Calendar) -> [String: Double] {
        let cutoff = current.time, day = calendar.startOfDay(for: cutoff)
        let hr = snapshot.heart.filter { $0.time >= day && $0.time < cutoff && $0.bpm.isFinite && $0.bpm > 0 }.map(\.bpm)
        let late = snapshot.heart.filter { $0.time >= cutoff.addingTimeInterval(-10800) && $0.time < cutoff && $0.bpm.isFinite && $0.bpm > 0 }.map(\.bpm)
        // Never consume an interval whose total was only available after the decision.
        let steps = snapshot.steps.filter { $0.start >= day && $0.end <= cutoff && $0.end > $0.start && $0.count.isFinite && $0.count >= 0 }
        var f = ["current_s": current.s, "current_c": current.c, "current_propensity": current.propensity,
                 "weekend": calendar.isDateInWeekend(cutoff) ? 1.0 : 0.0]
        func mean(_ a: [Double]) -> Double? { a.isEmpty ? nil : a.reduce(0, +) / Double(a.count) }
        f["day_hr_mean_pre20"] = mean(hr)
        if let m = mean(hr), hr.count > 1 { f["day_hr_std_pre20"] = sqrt(hr.reduce(0) { $0 + pow($1 - m, 2) } / Double(hr.count - 1)) }
        f["late_hr_mean_pre20"] = mean(late)
        if !steps.isEmpty {
            f["steps_pre20"] = steps.reduce(0) { $0 + $1.count }
            f["active_minutes_pre20"] = steps.filter { $0.count > 0 }.reduce(0) { $0 + $1.end.timeIntervalSince($1.start) / 60 }
        }
        let past = Array(episodes.filter { $0.end <= cutoff }.suffix(7))
        func clockMedian(_ dates: [Date], anchor: Double) -> Double? {
            HealthMath.median(dates.map { (HealthMath.clockMinutes($0, calendar: calendar) - anchor + 1440).truncatingRemainder(dividingBy: 1440) })
                .map { ($0 + anchor).truncatingRemainder(dividingBy: 1440) }
        }
        f["prior_bedtime_median_7"] = clockMedian(past.map(\.start), anchor: 1200)
        f["prior_wake_median_7"] = clockMedian(past.map(\.end), anchor: 0)
        f["prior_duration_median_7"] = HealthMath.median(past.map { $0.sleepMinutes / 60 })
        f["previous_sleep_duration_h"] = past.last.map { $0.sleepMinutes / 60 }
        f["caffeine_mg"] = survey.caffeineMg
        f["caffeine_gap_min"] = survey.caffeineTime.flatMap { $0 <= cutoff ? cutoff.timeIntervalSince($0) / 60 : nil }
        f["nap_minutes"] = survey.napMinutes
        f["nap_end_gap_min"] = survey.napEnd.flatMap { $0 <= cutoff ? cutoff.timeIntervalSince($0) / 60 : nil }
        f["exercise_minutes"] = survey.exerciseMinutes; f["exercise_intensity"] = survey.exerciseIntensity
        f["fatigue_score"] = survey.fatigue
        f["target_wake_minute"] = targetWake.map { HealthMath.clockMinutes($0, calendar: calendar) }
        return f.filter { $0.value.isFinite }
    }
}
