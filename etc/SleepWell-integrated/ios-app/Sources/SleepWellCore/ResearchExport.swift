import Foundation

public enum ResearchExport {
    private static func cell(_ text: String) -> String {
        "\"" + text.replacingOccurrences(of: "\"", with: "\"\"") + "\""
    }
    public static func epochCSV(_ snapshot: HealthSnapshot, calendar: Calendar = .current) -> String {
        let iso = ISO8601DateFormatter()
        iso.timeZone = calendar.timeZone
        let segments = snapshot.normalizedSleep()
        var lines = ["subject_id,night,session_id,epoch_idx,stage_label,time_iso,hr_mean,steps,sleep_wake,apple_stage,source"]
        guard let first = segments.first, let last = segments.last else { return lines.joined(separator: "\n") + "\n" }
        var time = Date(timeIntervalSince1970: floor(first.start.timeIntervalSince1970 / 30) * 30)
        var idx = 0
        let heart = snapshot.heart.sorted { $0.time < $1.time }
        let steps = snapshot.steps.sorted { $0.start < $1.start }
        var hi = 0, si = 0
        while time < last.end {
            let end = time.addingTimeInterval(30)
            while idx < segments.count && segments[idx].end <= time { idx += 1 }
            let overlaps = segments[idx...].prefix { $0.start < end }
            let stage = overlaps.max { a, b in
                let da = min(a.end, end).timeIntervalSince(max(a.start, time))
                let db = min(b.end, end).timeIntervalSince(max(b.start, time))
                return da == db ? a.stage.priority < b.stage.priority : da < db
            }
            if let stage {
                var noon = calendar.date(bySettingHour: 12, minute: 0, second: 0, of: time)!
                if time < noon { noon = calendar.date(byAdding: .day, value: -1, to: noon)! }
                let date = calendar.dateComponents([.year, .month, .day], from: noon)
                let night = String(format: "%04d%02d%02d", date.year!, date.month!, date.day!)
                let epoch = Int(time.timeIntervalSince(noon) / 30)
                while hi < heart.count && heart[hi].time < time { hi += 1 }
                let values = heart[hi...].prefix { $0.time < end }.map(\.bpm).filter(\.isFinite)
                let hr = values.isEmpty ? "" : String(values.reduce(0, +) / Double(values.count))
                while si < steps.count && steps[si].end <= time { si += 1 }
                let stepValues = steps[si...].prefix { $0.start < end }.filter { $0.end > time && $0.end > $0.start }
                let count = stepValues.reduce(0.0) { total, sample in
                    total + sample.count * min(sample.end, end).timeIntervalSince(max(sample.start, time)) / sample.end.timeIntervalSince(sample.start)
                }
                let sleepWake = stage.stage == .inBed ? "" : (stage.stage.isSleep ? "1" : "0")
                lines.append(["LOCAL", night, "apple-" + night, String(epoch), String(stage.stage.researchCode),
                              iso.string(from: time), hr, stepValues.isEmpty ? "" : String(count), sleepWake,
                              stage.stage.rawValue, snapshot.source].map(cell).joined(separator: ","))
            }
            time = end
        }
        return lines.joined(separator: "\n") + "\n"
    }

    public static func files(snapshot: HealthSnapshot, analysis: Analysis?, calendar: Calendar = .current) throws -> [String: Data] {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        struct Envelope: Encodable {
            let schema_version = 1
            let feature_contract = FeatureBuilder.contract
            let timezone: String
            let exported_at: Date
            let is_synthetic: Bool
            let stage_note = "Apple Core and unspecified sleep have stage_label=-1; use sleep_wake for binary evaluation. Steps in epochs are prorated, not measurements."
            let snapshot: HealthSnapshot
            let analysis: Analysis?
        }
        let mark = snapshot.source == "demo" ? "_DEMO" : ""
        return ["epoch_30s\(mark).csv": Data(epochCSV(snapshot, calendar: calendar).utf8),
                "snapshot\(mark).json": try encoder.encode(Envelope(timezone: calendar.timeZone.identifier,
                    exported_at: Date(), is_synthetic: snapshot.source == "demo", snapshot: snapshot, analysis: analysis))]
    }
}
