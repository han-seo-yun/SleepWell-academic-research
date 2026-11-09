import SwiftUI
import Charts
import UniformTypeIdentifiers
import SleepWellCore

struct ContentView: View {
    @StateObject private var model = AppModel()
    @State private var importing = false
    private let tint = Color(red: 0.38, green: 0.32, blue: 0.85)

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("오늘의 리듬을 이해하는 수면 기록", systemImage: "moon.stars.fill")
                        .font(.headline).foregroundStyle(tint)
                    Text("최근 20:00 기준으로 과거 기록을 분석합니다. 연구용 추정이며 권장 시각의 효과는 아직 검증되지 않았습니다.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("데이터 연결") {
                    Toggle("샘플 데이터로 둘러보기", isOn: $model.demo).onChange(of: model.demo) { _ in model.clearResults() }
                    if !model.demo {
                        Toggle("Apple Watch 기록만", isOn: $model.watchOnly).onChange(of: model.watchOnly) { _ in model.clearResults() }
                        Text("수면·심박·걸음을 읽습니다. 건강 앱에 동기화된 기록이 필요하며, 조회되는 항목이 없으면 읽기 권한도 확인하세요.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Button { Task { await model.refresh() } } label: {
                        HStack { Label(model.demo ? "샘플 분석하기" : "건강 기록 연결·분석", systemImage: "arrow.clockwise")
                            Spacer(); if model.busy { ProgressView() } }
                    }.disabled(model.busy)
                }.disabled(model.busy)
                Section("예측 시점까지의 설문 · 선택 입력") {
                    Text("아래 값은 화면에 표시되는 기준일의 20:00 이전 값만 입력하세요. 빈칸은 미응답으로 유지합니다. 변경 후 다시 분석하세요.")
                        .font(.caption).foregroundStyle(.secondary)
                    numberField("카페인 (mg)", text: $model.caffeine)
                    numberField("낮잠 (분)", text: $model.nap)
                    numberField("운동 (분)", text: $model.exercise)
                    numberField("피로도 (0~10)", text: $model.fatigue)
                    Toggle("목표 기상 시각 설정", isOn: $model.targetEnabled)
                    if model.targetEnabled { DatePicker("목표 기상", selection: $model.targetWake) }
                }.disabled(model.busy)
                if let error = model.error {
                    Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                }
                if let result = model.analysis {
                    Section("수면 시각 추정") {
                        Text(model.snapshot?.source == "demo" ? "샘플 분석 · 실제 예측이 아닙니다" : "HealthKit 기록 분석")
                            .font(.caption.bold()).foregroundStyle(tint)
                        LabeledContent("기준 시각", value: result.decision.formatted(date: .abbreviated, time: .shortened))
                        LabeledContent("계산 방식", value: result.modelVersion == nil ? "물리모델 · 학습 모델 없음" : "물리 + Ridge")
                        LabeledContent("추정 취침", value: format(result.predictedBed))
                        LabeledContent("추정 기상", value: format(result.predictedWake))
                        LabeledContent("일정 제안 · 취침", value: format(result.recommendedBed))
                        LabeledContent("일정 제안 · 기상", value: format(result.recommendedWake))
                        Text("나중에 다시 계산한 결과는 과거 시점 재현입니다. 실시간 예측 성능 평가와 구분하여 기록됩니다.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Section("앞으로 24시간 · 계속 깨어 있을 때") {
                        Chart(result.future) { point in
                            LineMark(x: .value("시각", point.time), y: .value("수면압력", point.s))
                                .foregroundStyle(by: .value("과정", "S · 수면압력"))
                            LineMark(x: .value("시각", point.time), y: .value("일주기", point.c))
                                .foregroundStyle(by: .value("과정", "C · 일주기"))
                        }.frame(height: 200)
                    }
                    Section("기록과 해석") {
                        if let snapshot = model.snapshot {
                            LabeledContent("심박 표본", value: "\(snapshot.heart.count)개")
                            LabeledContent("걸음 구간", value: "\(snapshot.steps.count)개")
                            LabeledContent("수면 구간", value: "\(snapshot.normalizedSleep().count)개")
                            let stages = snapshot.normalizedSleep().filter { $0.end > result.decision.addingTimeInterval(-86400) }
                            ForEach(SleepStage.allCases, id: \.self) { stage in
                                let minutes = stages.filter { $0.stage == stage }.reduce(0.0) { $0 + $1.end.timeIntervalSince(max($1.start, result.decision.addingTimeInterval(-86400))) / 60 }
                                if minutes > 0 { LabeledContent(stage.label, value: "\(Int(minutes))분") }
                            }
                        }
                        ForEach(Array(result.warnings.enumerated()), id: \.offset) { _, text in Text(text).font(.caption).foregroundStyle(.secondary) }
                    }
                }
                Section("학습 모델") {
                    LabeledContent("현재 모델", value: model.modelName)
                    Button("학습 완료 Ridge JSON 가져오기") { importing = true }
                    Button("물리모델만 사용") { model.usePhysicsOnly() }
                    Text("JSON 계수 계산을 지원합니다. 학습된 파일은 기본 제공되지 않습니다. 가져온 모델은 iOS 입력 규격과 별도 성능 검증이 필요합니다.")
                        .font(.caption).foregroundStyle(.secondary)
                }.disabled(model.busy)
                Section("연구용 내보내기") {
                    Text("30초 CSV, 원본 구간·예측 JSON, 예측 이력을 공유합니다. Core는 N1/N2로 변환하지 않습니다.")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("내보낼 파일 준비") { model.export() }.disabled(model.snapshot == nil || model.busy)
                    if !model.exportURLs.isEmpty { ShareLink(items: model.exportURLs) { Label("파일 공유·저장", systemImage: "square.and.arrow.up") } }
                }
            }
            .navigationTitle("SleepWell")
            .tint(tint)
            .fileImporter(isPresented: $importing, allowedContentTypes: [.json]) { result in
                switch result { case .success(let url): model.importModel(url); case .failure(let error): model.error = error.localizedDescription }
            }
        }
    }
    private func format(_ date: Date) -> String { date.formatted(date: .abbreviated, time: .shortened) }
    private func numberField(_ label: String, text: Binding<String>) -> some View {
        HStack { Text(label); Spacer(); TextField("미입력", text: text).keyboardType(.decimalPad).multilineTextAlignment(.trailing).frame(width: 100) }
    }
}
