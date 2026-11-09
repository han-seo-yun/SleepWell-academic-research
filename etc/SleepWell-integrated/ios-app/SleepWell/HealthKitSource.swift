import Foundation
import HealthKit
import SleepWellCore

/// Read-only access. HealthKit intentionally does not reveal whether read access was denied.
final class HealthKitSource {
    private let store = HKHealthStore()
    private let heartType = HKQuantityType.quantityType(forIdentifier: .heartRate)!
    private let stepType = HKQuantityType.quantityType(forIdentifier: .stepCount)!
    private let sleepType = HKCategoryType.categoryType(forIdentifier: .sleepAnalysis)!

    func authorize() async throws {
        guard HKHealthStore.isHealthDataAvailable() else {
            throw ModelError.invalid("이 기기에서 건강 데이터를 사용할 수 없습니다. 샘플 모드로 확인할 수 있습니다.")
        }
        try await store.requestAuthorization(toShare: [], read: [heartType, stepType, sleepType])
    }

    private func samples(_ type: HKSampleType, start: Date, end: Date) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: [.strictEndDate])
            let query = HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit,
                                      sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]) { _, samples, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: samples ?? []) }
            }
            store.execute(query)
        }
    }

    private func isWatch(_ sample: HKSample) -> Bool {
        let product = sample.sourceRevision.productType ?? ""
        let model = sample.device?.model ?? ""
        return product.lowercased().hasPrefix("watch") || model.lowercased().contains("watch")
    }
    private func sourceKey(_ sample: HKSample) -> String {
        sample.sourceRevision.source.bundleIdentifier + ":" + (sample.sourceRevision.productType ?? "unknown")
    }
    private func select(_ samples: [HKSample], watchOnly: Bool, warnings: inout [String], label: String) -> [HKSample] {
        let filtered = samples.filter {
            (!watchOnly || isWatch($0)) && ($0.metadata?[HKMetadataKeyWasUserEntered] as? Bool != true)
        }
        let grouped = Dictionary(grouping: filtered, by: sourceKey)
        // One source per measurement type avoids adding phone + Watch totals together.
        let chosen = grouped.keys.sorted().max { (grouped[$0]?.count ?? 0) < (grouped[$1]?.count ?? 0) }
        if grouped.count > 1 { warnings.append("\(label): 중복 합산 방지를 위해 표본이 가장 많은 단일 소스를 선택했습니다: \(chosen ?? "")") }
        guard let chosen else {
            warnings.append("\(label): 읽을 수 있는 기록이 없습니다. 기록 부재·읽기 권한·Watch 동기화 상태를 확인하세요.")
            return []
        }
        var ids = Set<UUID>()
        return grouped[chosen]!.filter { ids.insert($0.uuid).inserted }
    }

    func read(start: Date, end: Date, watchOnly: Bool) async throws -> HealthSnapshot {
        var warnings: [String] = []
        var hr: [HKSample] = [], st: [HKSample] = [], sl: [HKSample] = []
        do { hr = try await samples(heartType, start: start, end: end) } catch { warnings.append("심박 조회 실패: \(error.localizedDescription)") }
        do { st = try await samples(stepType, start: start, end: end) } catch { warnings.append("걸음 조회 실패: \(error.localizedDescription)") }
        do { sl = try await samples(sleepType, start: start, end: end) } catch { warnings.append("수면 조회 실패: \(error.localizedDescription)") }
        let heart = select(hr, watchOnly: watchOnly, warnings: &warnings, label: "심박").compactMap { sample -> HeartSample? in
            guard let sample = sample as? HKQuantitySample else { return nil }
            let value = sample.quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute()))
            guard value.isFinite, value > 0 else { return nil }
            return .init(time: sample.startDate, bpm: value, source: sourceKey(sample))
        }
        let steps = select(st, watchOnly: watchOnly, warnings: &warnings, label: "걸음").compactMap { sample -> StepSample? in
            guard let sample = sample as? HKQuantitySample, sample.endDate > sample.startDate else { return nil }
            let count = sample.quantity.doubleValue(for: .count())
            guard count.isFinite, count >= 0 else { return nil }
            return .init(start: sample.startDate, end: sample.endDate, count: count, source: sourceKey(sample))
        }
        let sleep = select(sl, watchOnly: watchOnly, warnings: &warnings, label: "수면").compactMap { sample -> SleepSegment? in
            guard let sample = sample as? HKCategorySample else { return nil }
            let stage: SleepStage
            switch sample.value {
            case HKCategoryValueSleepAnalysis.awake.rawValue: stage = .awake
            case HKCategoryValueSleepAnalysis.asleepCore.rawValue: stage = .core
            case HKCategoryValueSleepAnalysis.asleepDeep.rawValue: stage = .deep
            case HKCategoryValueSleepAnalysis.asleepREM.rawValue: stage = .rem
            case HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue: stage = .asleep
            case HKCategoryValueSleepAnalysis.inBed.rawValue: stage = .inBed
            default: return nil
            }
            return .init(start: sample.startDate, end: sample.endDate, stage: stage, source: sourceKey(sample))
        }
        return .init(source: "healthkit", start: start, end: end, sleep: sleep, heart: heart, steps: steps, warnings: warnings)
    }
}
