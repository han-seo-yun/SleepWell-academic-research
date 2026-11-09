import Foundation
import SwiftUI
import SleepWellCore

@MainActor
final class AppModel: ObservableObject {
    @Published var demo = true
    @Published var watchOnly = true
    @Published var busy = false
    @Published var error: String?
    @Published var snapshot: HealthSnapshot?
    @Published var analysis: Analysis?
    @Published var modelName = "미학습 · 물리모델"
    @Published var exportURLs: [URL] = []
    @Published var targetEnabled = false
    @Published var targetWake = Calendar.current.date(bySettingHour: 7, minute: 0, second: 0, of: Date().addingTimeInterval(86400))!
    @Published var caffeine = ""
    @Published var nap = ""
    @Published var exercise = ""
    @Published var fatigue = ""
    private var ridge: RidgeModel?
    private let health = HealthKitSource()
    private let files = FileManager.default
    private var appFolder: URL { files.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("SleepWell", isDirectory: true) }

    init() {
        let saved = appFolder.appendingPathComponent("ridge_residual.json")
        if files.fileExists(atPath: saved.path) {
            do { let m = try RidgeModel.load(Data(contentsOf: saved)); ridge = m; modelName = m.model_version }
            catch { self.error = "저장된 모델을 사용할 수 없습니다: \(error.localizedDescription)" }
        }
    }

    func clearResults() { snapshot = nil; analysis = nil; exportURLs = []; error = nil }

    func refresh() async {
        busy = true; error = nil; exportURLs = []; analysis = nil; snapshot = nil
        defer { busy = false }
        do {
            let calendar = Calendar.current, now = Date()
            // Latest completed 20:00 decision, with an explicit timestamp on screen/export.
            let today = calendar.date(bySettingHour: 20, minute: 0, second: 0, of: now)!
            let decision = today <= now ? today : calendar.date(byAdding: .day, value: -1, to: today)!
            let start = calendar.date(byAdding: .day, value: -7, to: calendar.startOfDay(for: decision))!
            let data: HealthSnapshot
            if demo { data = .demo(at: decision, calendar: calendar) }
            else { try await health.authorize(); data = try await health.read(start: start, end: decision, watchOnly: watchOnly) }
            snapshot = data
            func number(_ value: String, name: String, maximum: Double) throws -> Double? {
                if value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return nil }
                guard let x = Double(value), x.isFinite, x >= 0, x <= maximum else { throw ModelError.invalid("\(name) 입력 범위를 확인하세요 (0~\(Int(maximum))).") }
                return x
            }
            let survey = try Survey(caffeineMg: number(caffeine, name: "카페인", maximum: 2000),
                                    napMinutes: number(nap, name: "낮잠", maximum: 1440),
                                    exerciseMinutes: number(exercise, name: "운동", maximum: 1440),
                                    fatigue: number(fatigue, name: "피로도", maximum: 10))
            let result = try TwoProcess.analyze(data, decision: decision, calendar: calendar,
                                               survey: survey, targetWake: targetEnabled ? targetWake : nil, model: ridge)
            analysis = result
            // Local audit log records when a prediction was actually generated. Retrospective runs are flagged.
            try files.createDirectory(at: appFolder, withIntermediateDirectories: true)
            struct Log: Encodable {
                let generated_at: Date
                let retrospective: Bool
                let synthetic: Bool
                let feature_contract: String
                let analysis: Analysis
            }
            let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
            let record = Log(generated_at: now, retrospective: now.timeIntervalSince(decision) > 300,
                             synthetic: demo, feature_contract: FeatureBuilder.contract, analysis: result)
            let logs = appFolder.appendingPathComponent("Predictions", isDirectory: true)
            try files.createDirectory(at: logs, withIntermediateDirectories: true)
            try encoder.encode(record).write(to: logs.appendingPathComponent(UUID().uuidString + ".json"), options: [.atomic, .completeFileProtection])
        } catch { self.error = error.localizedDescription }
    }

    func importModel(_ url: URL) {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try Data(contentsOf: url)
            let parsed = try RidgeModel.load(data)
            try files.createDirectory(at: appFolder, withIntermediateDirectories: true)
            try data.write(to: appFolder.appendingPathComponent("ridge_residual.json"), options: [.atomic, .completeFileProtection])
            ridge = parsed; modelName = parsed.model_version; analysis = nil; exportURLs = []; error = nil
        } catch { self.error = "모델 가져오기 실패: \(error.localizedDescription)" }
    }

    func usePhysicsOnly() {
        do {
            let saved = appFolder.appendingPathComponent("ridge_residual.json")
            if files.fileExists(atPath: saved.path) { try files.removeItem(at: saved) }
            ridge = nil; modelName = "미학습 · 물리모델"; analysis = nil; exportURLs = []; error = nil
        } catch { self.error = error.localizedDescription }
    }

    func export() {
        guard let snapshot else { return }
        do {
            let folder = files.temporaryDirectory.appendingPathComponent("SleepWell-" + UUID().uuidString, isDirectory: true)
            try files.createDirectory(at: folder, withIntermediateDirectories: true)
            let contents = try ResearchExport.files(snapshot: snapshot, analysis: analysis)
            var urls: [URL] = []
            for name in contents.keys.sorted() {
                let url = folder.appendingPathComponent(name)
                try contents[name]!.write(to: url, options: [.atomic, .completeFileProtection]); urls.append(url)
            }
            // Include saved predictions for prospective versus retrospective evaluation.
            let logs = appFolder.appendingPathComponent("Predictions", isDirectory: true)
            if files.fileExists(atPath: logs.path) {
                let records = try files.contentsOfDirectory(at: logs, includingPropertiesForKeys: nil).filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
                let lines = try records.map { try String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
                let url = folder.appendingPathComponent("prediction_log.jsonl")
                try Data((lines + "\n").utf8).write(to: url, options: [.atomic, .completeFileProtection]); urls.append(url)
            }
            exportURLs = urls
        } catch { self.error = "내보내기 실패: \(error.localizedDescription)" }
    }
}
