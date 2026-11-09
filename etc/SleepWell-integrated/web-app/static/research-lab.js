(() => {
  "use strict";

  const DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024;
  const AUTO_RUN_DELAY_MS = 700;
  const PNG_PREFIX = "data:image/png;base64,";

  const byId = (id) => document.getElementById(id);
  const ui = {
    sessionState: byId("session-state"),
    backToApp: byId("back-to-app"),
    integrationState: byId("integration-state"),
    integrationSource: byId("integration-source"),
    importAppRecords: byId("import-app-records"),
    importSummary: byId("import-summary"),
    evidenceBanner: byId("evidence-banner"),
    evidenceLabel: byId("evidence-label"),
    evidenceDetail: byId("evidence-detail"),
    realPanel: byId("real-panel"),
    syntheticPanel: byId("synthetic-panel"),
    csvInputPanel: byId("csv-input-panel"),
    jsonInputPanel: byId("json-input-panel"),
    csvFile: byId("csv-file"),
    csvText: byId("csv-text"),
    recordsJson: byId("records-json"),
    autoRun: byId("auto-run"),
    runButton: byId("run-evaluation"),
    inputError: byId("input-error"),
    runStatus: byId("run-status"),
    modelStatus: byId("model-status"),
    downloadTemplate: byId("download-template"),
    downloadResult: byId("download-result"),
    downloadPublication: byId("download-publication"),
    currentResultState: byId("current-result-state"),
    summaryCards: byId("summary-cards"),
    predictionOnlyNote: byId("prediction-only-note"),
    modelResultsPanel: byId("model-results-panel"),
    modelResults: byId("model-results"),
    contextPanel: byId("context-panel"),
    contextSummary: byId("context-summary"),
    metricPanel: byId("metric-panel"),
    metrics: byId("metrics"),
    metricsDetail: byId("metrics-detail"),
    availableMetrics: byId("available-metrics"),
    chartPanel: byId("chart-panel"),
    chartParticipant: byId("chart-participant"),
    charts: byId("charts"),
    diagnosticChartsPanel: byId("diagnostic-charts-panel"),
    diagnosticCharts: byId("diagnostic-charts"),
    observationPanel: byId("observation-panel"),
    observationRegularity: byId("observation-regularity"),
    observationCompleteness: byId("observation-completeness"),
    observationCharts: byId("observation-charts"),
    observationAssumptions: byId("observation-assumptions"),
    personalizationPanel: byId("personalization-panel"),
    personalizationParticipant: byId("personalization-participant"),
    personalizationCutoff: byId("personalization-cutoff"),
    personalization: byId("personalization"),
    warningPanel: byId("warning-panel"),
    warnings: byId("warnings"),
  };

  const state = {
    token: null,
    maxBodyBytes: DEFAULT_MAX_BODY_BYTES,
    revision: 0,
    inFlight: false,
    exporting: false,
    queuedJob: null,
    autoTimer: null,
    lastResult: null,
    lastPayload: null,
    personalizationRows: [],
    integration: null,
  };

  function createElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function selectedRadio(name) {
    return document.querySelector(`input[name="${name}"]:checked`)?.value || "";
  }

  function setEvidence(label, detail, tone) {
    // The evidence banner was removed from the lab screen; keep callers harmless.
    if (!ui.evidenceBanner) return;
    ui.evidenceLabel.textContent = label;
    ui.evidenceDetail.textContent = detail;
    ui.evidenceBanner.className = `evidence-banner evidence-${tone}`;
  }

  function setInputError(message) {
    ui.inputError.textContent = message || "";
    ui.inputError.hidden = !message;
  }

  function setRunStatus(message) {
    ui.runStatus.textContent = message;
  }

  function syncInputPanels() {
    const mode = selectedRadio("mode");
    ui.realPanel.hidden = mode !== "real";
    ui.syntheticPanel.hidden = mode !== "synthetic";
    const realFormat = selectedRadio("real-format");
    ui.csvInputPanel.hidden = realFormat !== "csv";
    ui.jsonInputPanel.hidden = realFormat !== "json";
  }

  function clearResultDisplay() {
    ui.summaryCards.replaceChildren();
    ui.modelResults.replaceChildren();
    ui.metrics.replaceChildren();
    ui.metricsDetail.replaceChildren();
    ui.availableMetrics.replaceChildren();
    ui.charts.replaceChildren();
    ui.diagnosticCharts.replaceChildren();
    ui.observationRegularity.replaceChildren();
    ui.observationCompleteness.replaceChildren();
    ui.observationCharts.replaceChildren();
    ui.observationAssumptions.replaceChildren();
    ui.observationPanel.hidden = true;
    ui.personalization.replaceChildren();
    ui.warnings?.replaceChildren();
    ui.chartParticipant.replaceChildren();
    ui.personalizationParticipant.replaceChildren();
    ui.personalizationCutoff.replaceChildren();
    ui.modelResultsPanel.hidden = true;
    ui.metricPanel.hidden = true;
    ui.chartPanel.hidden = true;
    ui.diagnosticChartsPanel.hidden = true;
    ui.personalizationPanel.hidden = true;
    ui.contextPanel.hidden = true;
    if (ui.warningPanel) ui.warningPanel.hidden = true;
    ui.predictionOnlyNote.hidden = true;
    ui.predictionOnlyNote.textContent = "";
    ui.chartParticipant.disabled = true;
    state.personalizationRows = [];
  }

  function invalidateCurrentResult(message) {
    state.lastResult = null;
    state.lastPayload = null;
    ui.downloadResult.disabled = true;
    ui.downloadPublication.disabled = true;
    clearResultDisplay();
    ui.currentResultState.textContent = message;
    ui.currentResultState.removeAttribute("title");
  }

  function scheduleAutomaticRun() {
    window.clearTimeout(state.autoTimer);
    state.autoTimer = window.setTimeout(() => {
      requestFormEvaluation("자동 재계산");
    }, AUTO_RUN_DELAY_MS);
  }

  function payloadChanged() {
    state.revision += 1;
    state.queuedJob = null;
    setInputError("");
    invalidateCurrentResult("입력이 변경되어 이전 결과를 현재 결과로 표시하지 않습니다.");
    setEvidence(
      "입력 변경됨 — 현재 결과 없음",
      "현재 입력을 평가해야 새 근거 라벨과 그림을 표시합니다.",
      "neutral",
    );
    setRunStatus(state.inFlight ? "이전 계산이 끝나면 최신 입력을 평가할 수 있습니다." : "현재 입력은 아직 평가하지 않았습니다.");
    if (ui.autoRun.checked) scheduleAutomaticRun();
  }

  function inputNumber(id, label, integer = false) {
    const input = byId(id);
    const value = input.valueAsNumber;
    if (!input.validity.valid || !Number.isFinite(value) || (integer && !Number.isInteger(value))) {
      throw new Error(`${label} 값을 허용 범위에 맞게 입력해주세요.`);
    }
    return value;
  }

  function buildPayload() {
    const cohorts = [byId("cohort-cmi"), byId("cohort-mesa")]
      .filter((control) => control.checked)
      .map((control) => control.value);
    if (cohorts.length === 0) throw new Error("비교할 코호트 모델을 하나 이상 선택해주세요.");

    const mode = selectedRadio("mode");
    const payload = {schema_version: 1, mode, cohorts};
    if (mode === "real") {
      if (selectedRadio("real-format") === "csv") {
        const csvText = ui.csvText.value;
        if (!csvText.trim()) throw new Error("CSV 파일을 읽거나 CSV 내용을 입력해주세요.");
        payload.csv_text = csvText;
      } else {
        const source = ui.recordsJson.value.trim();
        if (!source) throw new Error("레코드 JSON 배열을 입력해주세요.");
        let records;
        try {
          records = JSON.parse(source);
        } catch (_error) {
          throw new Error("레코드 편집기의 JSON 형식을 확인해주세요.");
        }
        if (Array.isArray(records)) {
          payload.records = records;
        } else if (records && typeof records === "object" && records.payload && typeof records.payload === "object") {
          const imported = records.payload;
          if (!Array.isArray(imported.records)) throw new Error("연동 스냅샷의 records 배열이 없습니다.");
          payload.records = imported.records;
          if (Array.isArray(imported.context)) payload.context = imported.context;
        } else {
          throw new Error("레코드 JSON은 배열 또는 연동 스냅샷 객체여야 합니다.");
        }
      }
      return payload;
    }

    if (mode !== "synthetic") throw new Error("입력 근거를 선택해주세요.");
    const startDate = byId("syn-start-date").value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(startDate)) throw new Error("가상 데이터 시작일을 입력해주세요.");
    const nNights = inputNumber("syn-n-nights", "야간 수", true);
    const shiftAfterNight = inputNumber("syn-shift-after-night", "이동 시작 야간", true);
    if (shiftAfterNight > nNights) throw new Error("이동 시작 야간은 전체 야간 수보다 클 수 없습니다.");
    payload.synthetic = {
      n_nights: nNights,
      seed: inputNumber("syn-seed", "난수 시드", true),
      start_date: startDate,
      bed_hour: inputNumber("syn-bed-hour", "취침 중심 시각"),
      duration_hours: inputNumber("syn-duration-hours", "수면 시간"),
      jitter_minutes: inputNumber("syn-jitter-minutes", "변동 폭"),
      phase_shift_minutes: inputNumber("syn-phase-shift-minutes", "위상 이동"),
      shift_after_night: shiftAfterNight,
      missing_every: inputNumber("syn-missing-every", "누락 주기", true),
      report_delay_hours: inputNumber("syn-report-delay-hours", "보고 지연"),
    };
    return payload;
  }

  function cloneJSON(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function markEvaluationPending(reason) {
    ui.downloadResult.disabled = true;
    ui.downloadPublication.disabled = true;
    ui.currentResultState.textContent = `${reason} 중입니다. 표시된 내용이 있다면 직전 실행 결과입니다.`;
    setEvidence(
      "계산 중 — 새 결과 대기",
      "현재 요청이 끝날 때까지 직전 결과를 현재 결과로 해석하지 마세요.",
      "pending",
    );
    setRunStatus("모델 예측과 Python Matplotlib 그림을 계산하고 있습니다.");
    document.body.classList.add("evaluation-pending");
  }

  function requestFormEvaluation(reason) {
    window.clearTimeout(state.autoTimer);
    setInputError("");
    let payload;
    try {
      payload = buildPayload();
    } catch (error) {
      const message = error instanceof Error ? error.message : "입력을 확인해주세요.";
      setInputError(message);
      invalidateCurrentResult("입력이 유효하지 않아 현재 결과가 없습니다.");
      setEvidence("입력 오류 — 평가 안 됨", message, "error");
      setRunStatus("입력을 수정한 뒤 다시 평가해주세요.");
      return;
    }
    enqueueEvaluation({payload, revision: state.revision, reason});
  }

  function enqueueEvaluation(job) {
    if (!state.token) {
      setInputError("로컬 세션 토큰을 불러오지 못했습니다. 페이지를 새로고침해주세요.");
      return;
    }
    if (state.inFlight) {
      state.queuedJob = job;
      setRunStatus("진행 중인 한 건이 끝나면 가장 최근 요청만 이어서 계산합니다.");
      return;
    }
    void executeEvaluation(job);
  }

  async function responseJSON(response) {
    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch (_error) {
      throw new Error(`서버가 JSON이 아닌 응답을 보냈습니다(HTTP ${response.status}).`);
    }
    if (!response.ok) {
      const message = data && typeof data.error === "string" ? data.error : `평가 요청이 실패했습니다(HTTP ${response.status}).`;
      throw new Error(message);
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("평가 응답 형식이 올바르지 않습니다.");
    return data;
  }

  async function executeEvaluation(job) {
    state.inFlight = true;
    markEvaluationPending(job.reason);
    try {
      const response = await fetch("/api/lab/evaluate", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-SleepWell-Token": state.token,
        },
        body: JSON.stringify(job.payload),
      });
      const result = await responseJSON(response);
      if (state.revision !== job.revision) {
        invalidateCurrentResult("계산 중 입력이 변경되어 완료된 응답을 현재 결과로 표시하지 않았습니다.");
        setEvidence(
          "입력 변경됨 — 이전 응답 폐기",
          "최신 입력을 다시 평가해야 합니다.",
          "neutral",
        );
        setRunStatus(ui.autoRun.checked ? "최신 입력 자동 평가를 준비합니다." : "현재 입력을 다시 평가해주세요.");
        return;
      }
      renderResult(result, job.payload);
      state.lastResult = result;
      state.lastPayload = cloneJSON(job.payload);
      ui.downloadResult.disabled = false;
      ui.downloadPublication.disabled = state.exporting;
    } catch (error) {
      if (state.revision === job.revision) {
        const message = error instanceof Error ? error.message : "평가 요청을 완료하지 못했습니다.";
        setInputError(message);
        invalidateCurrentResult("평가가 실패하여 현재 입력에 연결된 결과가 없습니다.");
        setEvidence("평가 실패 — 현재 결과 없음", message, "error");
        setRunStatus("입력과 로컬 모델 상태를 확인한 뒤 다시 평가해주세요.");
      }
    } finally {
      state.inFlight = false;
      document.body.classList.remove("evaluation-pending");
      const queued = state.queuedJob;
      state.queuedJob = null;
      if (queued) window.setTimeout(() => executeEvaluation(queued), 0);
    }
  }

  function displayValue(value) {
    if (value === null || value === undefined) return "결측(null)";
    if (typeof value === "boolean") return value ? "예" : "아니요";
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return "유효하지 않은 수";
      return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(6)));
    }
    if (typeof value === "object") {
      try {
        return JSON.stringify(value);
      } catch (_error) {
        return "표시할 수 없는 객체";
      }
    }
    return String(value);
  }

  function metricValue(value, digits = 1) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "계산 불가";
  }

  function renderTable(container, columns, rows, emptyMessage) {
    container.replaceChildren();
    if (!Array.isArray(rows) || rows.length === 0) {
      container.append(createElement("p", "empty-state", emptyMessage));
      return;
    }
    const table = createElement("table", "data-table");
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const column of columns) {
      const th = createElement("th", "", column.label);
      th.scope = "col";
      headerRow.append(th);
    }
    thead.append(headerRow);
    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const column of columns) {
        const td = document.createElement("td");
        const raw = typeof column.value === "function" ? column.value(row) : row[column.value];
        td.textContent = raw === null || raw === undefined || raw === "" ? "—" : String(raw);
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    container.append(table);
  }

  function renderSummary(result) {
    const summary = result.summary && typeof result.summary === "object" ? result.summary : {};
    const participants = Array.isArray(summary.participants) ? summary.participants : [];
    const cards = [
      ["근거 유형", result.mode === "synthetic" ? "가상 설명" : "실제 후향 재생"],
      ["입력 행", displayValue(summary.rows)],
      ["레이블 야간", displayValue(summary.labeled_nights)],
      ["공통 비교 야간", displayValue(summary.common_nights)],
      ["분석 참가자", participants.length ? `${participants.length}명` : "없음"],
    ];
    ui.summaryCards.replaceChildren();
    for (const [label, value] of cards) {
      const card = createElement("div", "summary-card");
      card.append(createElement("span", "summary-label", label), createElement("strong", "summary-value", value));
      ui.summaryCards.append(card);
    }

    const predictionOnly = summary.labeled_nights === 0;
    ui.predictionOnlyNote.hidden = !predictionOnly;
    if (predictionOnly) {
      ui.predictionOnlyNote.textContent = "예측 전용 입력입니다. 정답 레이블이 없어 오차 지표는 계산 불가이며, 예측값과 개인화 입력 상태만 표시합니다.";
    }

    ui.chartParticipant.replaceChildren();
    for (const participant of participants) {
      const option = createElement("option", "", participant);
      option.value = String(participant);
      ui.chartParticipant.append(option);
    }
    const chartParticipant = summary.chart_participant_id;
    if (chartParticipant !== null && chartParticipant !== undefined) {
      ui.chartParticipant.value = String(chartParticipant);
    }
    ui.chartParticipant.disabled = participants.length < 2;
  }

  function renderContext(context) {
    const rows = Array.isArray(context) ? context : [];
    if (!rows.length) { ui.contextPanel.hidden = true; ui.contextSummary.replaceChildren(); return; }
    const counts = {};
    for (const item of rows) {
      const kind = item && (item.kind || item.type || item.event_type) || "기타";
      counts[kind] = (counts[kind] || 0) + 1;
    }
    ui.contextSummary.replaceChildren();
    const grid = createElement("div", "context-summary-grid");
    for (const [kind, count] of Object.entries(counts)) {
      const card = createElement("div", "context-summary-item");
      card.append(createElement("span", "", kind), createElement("strong", "", `${count}건`));
      grid.append(card);
    }
    ui.contextSummary.append(grid);
    ui.contextPanel.hidden = false;
  }

  function renderContextAnalysis(summary, features) {
    const featureRows = Array.isArray(features) ? features : [];
    const summaryObject = summary && typeof summary === "object" ? summary : {};
    if (!featureRows.length && !Object.keys(summaryObject).length) return;
    ui.contextPanel.hidden = false;
    ui.contextSummary.replaceChildren();
    ui.contextSummary.append(createElement(
      "p",
      "source-note",
      "보조 맥락 자료의 품질과 가용성을 표시합니다. 현재 동결 예측 모델의 입력 또는 재학습 자료로 사용하지 않습니다.",
    ));
    if (typeof summaryObject.note === "string" && summaryObject.note) {
      ui.contextSummary.append(createElement("p", "source-note", summaryObject.note));
    }
    const cards = createElement("div", "context-summary-grid");
    const contextKinds = [["steps", "steps", "걸음"], ["exercise", "exercise_min", "운동"],
      ["caffeine", "caffeine_mg", "카페인"], ["schedule", "schedule_start_offset_min", "일정"], ["nap", "nap_min", "낮잠"]];
    let hasContextAvailability = false;
    for (const [kind, field, label] of contextKinds) {
      const count = summaryObject.coverage?.[kind] ?? 0;
      const nights = summaryObject.available_nights?.[field] ?? 0;
      if (!(Number(count) > 0 || Number(nights) > 0)) continue;
      hasContextAvailability = true;
      const card = createElement("div", "context-summary-item");
      card.append(createElement("span", "", label), createElement("strong", "", `${count}건 수신 · ${nights}개 야간에서 가용`));
      cards.append(card);
    }
    if (cards.children.length) ui.contextSummary.append(cards);
    else ui.contextSummary.append(createElement("p", "empty-state", "수신됐거나 분석 시점에 가용한 보조 맥락 자료가 없습니다."));
    if (featureRows.length && hasContextAvailability) {
      const columns = [
        {label: "참가자", value: "participant_id"}, {label: "결정 시각", value: "cutoff"},
        {label: "걸음", value: "steps"}, {label: "운동(분)", value: "exercise_min"},
        {label: "카페인(mg)", value: "caffeine_mg"}, {label: "20시→첫 일정(분)", value: "schedule_start_offset_min"},
        {label: "낮잠(분)", value: "nap_min"}, {label: "시점 제한으로 제외(건)", value: "unavailable_events"},
      ];
      const disclosure = createElement("details", "available-metrics-details");
      disclosure.append(createElement("summary", "", "야간별 보조 맥락 가용성 확인"));
      const tableRegion = createElement("div", "table-region");
      renderTable(tableRegion, columns, featureRows, "조건별 맥락 feature가 없습니다.");
      disclosure.append(tableRegion);
      ui.contextSummary.append(disclosure);
    }
  }

  function renderModels(result) {
    const models = Array.isArray(result.models) ? result.models : [];
    const metrics = Array.isArray(result.available_metrics) ? result.available_metrics : [];
    const metricsById = new Map(metrics.map((entry) => [entry.model_id, entry]));
    const rows = models.map((model) => {
      const sample = metricsById.get(model.id) || {};
      return {
        id: model.id,
        label: model.label,
        cohort: model.cohort,
        kind: model.kind,
        hash: model.artifact_sha256,
        n: sample.n,
        participants: sample.n_participants,
        error: model.error,
      };
    });
    const known = new Set(rows.map((row) => row.id));
    for (const metric of metrics) {
      if (!known.has(metric.model_id)) {
        rows.push({
          id: metric.model_id,
          label: metric.model_id === "baseline.recent7" ? "최근 7일 기준선" : metric.model_id,
          cohort: "공통",
          kind: "baseline",
          hash: null,
          n: metric.n,
          participants: metric.n_participants,
          error: null,
        });
      }
    }
    renderTable(
      ui.modelResults,
      [
        {label: "모델 ID", value: "id"},
        {label: "표시명", value: "label"},
        {label: "코호트", value: "cohort"},
        {label: "종류", value: "kind"},
        {label: "n", value: "n"},
        {label: "참가자", value: "participants"},
        {label: "artifact SHA-256", value: "hash"},
        {label: "오류", value: "error"},
      ],
      rows,
      "실행된 모델 메타데이터가 없습니다.",
    );
    ui.modelResultsPanel.hidden = false;
  }

  const metricColumns = [
    {label: "모델", value: "model_id"},
    {label: "n", value: "n"},
    {label: "참가자", value: "n_participants"},
    {label: "평균 방식", value: (row) => row.averaging === "participant_equal" ? "참가자 동일 가중" : displayValue(row.averaging)},
    {label: "입면 MAE(분)", value: (row) => metricValue(row.onset_mae_min)},
    {label: "기상 MAE(분)", value: (row) => metricValue(row.wake_mae_min)},
    {label: "구간 MAE(분)", value: (row) => metricValue(row.interval_mae_min)},
    {label: "결합 MAE(분)", value: (row) => metricValue(row.joint_mae_min)},
    {label: "입면 RMSE(분)", value: (row) => metricValue(row.onset_rmse_min)},
    {label: "기상 RMSE(분)", value: (row) => metricValue(row.wake_rmse_min)},
    {label: "최근7회 대비 skill", value: (row) => metricValue(row.skill_vs_recent7, 3)},
  ];

  function renderMetrics(result) {
    const modelLabels = new Map((Array.isArray(result.models) ? result.models : [])
      .filter((model) => model && typeof model.id === "string")
      .map((model) => {
        const label = String(model.label || model.id);
        const cohort = typeof model.cohort === "string" ? model.cohort.trim().toUpperCase() : "";
        const displayLabel = cohort && !label.toUpperCase().startsWith(cohort)
          ? `${cohort} · ${label}` : label;
        return [model.id, displayLabel];
      }));
    const modelName = (row) => modelLabels.get(row.model_id)
      || (row.model_id === "baseline.recent7" ? "최근 7일 기준선" : row.model_id);
    const primaryColumns = [
      {label: "모델", value: modelName},
      {label: "공통 n", value: "n"},
      {label: "참가자", value: "n_participants"},
      {label: "결합 MAE(분)", value: (row) => metricValue(row.joint_mae_min)},
      {label: "입면 MAE(분)", value: (row) => metricValue(row.onset_mae_min)},
      {label: "기상 MAE(분)", value: (row) => metricValue(row.wake_mae_min)},
      {label: "최근7회 대비 skill", value: (row) => metricValue(row.skill_vs_recent7, 3)},
    ];
    renderTable(
      ui.metrics,
      primaryColumns,
      Array.isArray(result.metrics) ? result.metrics : [],
      "공통 비교 가능한 레이블 야간이 없습니다. 지표를 0으로 대체하지 않습니다.",
    );
    renderTable(
      ui.metricsDetail,
      [{label: "모델", value: modelName}, ...metricColumns.slice(1)],
      Array.isArray(result.metrics) ? result.metrics : [],
      "공통 비교 가능한 세부 지표가 없습니다.",
    );
    renderTable(
      ui.availableMetrics,
      [{label: "모델", value: modelName}, ...metricColumns.slice(1)],
      Array.isArray(result.available_metrics) ? result.available_metrics : [],
      "모델별 계산 가능한 레이블 지표가 없습니다.",
    );
    ui.metricPanel.hidden = false;
  }

  function validPngDataUri(value) {
    if (typeof value !== "string" || !value.startsWith(PNG_PREFIX)) return false;
    const encoded = value.slice(PNG_PREFIX.length);
    return encoded.length > 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(encoded);
  }

  function renderCharts(result) {
    ui.charts.replaceChildren();
    ui.diagnosticCharts.replaceChildren();
    const charts = Array.isArray(result.charts) ? result.charts : [];
    const chartOrder = new Map([
      ["model-mae-comparison", 0], ["participant-mae-heatmap", 1],
      ["joint-error-distribution", 2], ["signed-residual-bias", 3],
      ["prediction-timeline", 4],
    ]);
    const observationChartIds = new Set(["sleep-actogram", "data-completeness"]);
    const primaryCharts = charts
      .filter((chart) => chart?.id !== "personalization-traces" && !observationChartIds.has(chart?.id))
      .sort((left, right) => (chartOrder.get(left?.id) ?? 99) - (chartOrder.get(right?.id) ?? 99));
    const diagnosticCharts = charts.filter((chart) => chart?.id === "personalization-traces");
    const observationCharts = charts.filter((chart) => observationChartIds.has(chart?.id));
    if (primaryCharts.length === 0) {
      ui.charts.append(createElement("p", "empty-state", "생성된 PNG 그림이 없습니다."));
    }
    const appendChart = (chart, container) => {
      const figure = createElement("figure", "chart-card");
      const title = createElement("figcaption", "chart-title", chart && chart.title ? chart.title : "제목 없는 그림");
      figure.append(title);
      const fallbackQuestions = {
        "prediction-timeline": "관측 시각을 모델 예측이 밤별로 얼마나 따라가는가?",
        "model-mae-comparison": "같은 평가 야간에서 어떤 모델의 시간 오차가 가장 작은가?",
        "personalization-traces": "개인화 입력과 내부 상태가 기록 축적에 따라 어떻게 변하는가?",
        "participant-mae-heatmap": "평균 성능이 일부 참가자에 의해 좌우되는가?",
        "joint-error-distribution": "참가자별 평균 오차의 분포는 어떤가?",
        "signed-residual-bias": "모델이 입면·기상 시각을 일관되게 이르게 또는 늦게 예측하는가?",
        "sleep-actogram": "실제 수면 시각이 날마다 얼마나 일정하고, 예측 구간이 그 변화를 따라가는가?",
        "data-completeness": "어느 참가자·날짜의 결과가 빠지거나 다음 20시 이후에 늦게 입력됐는가?",
      };
      const question = chart?.id === "joint-error-distribution"
        ? fallbackQuestions[chart.id]
        : (chart && typeof chart.description === "string" && chart.description
          ? chart.description : fallbackQuestions[chart?.id]);
      if (question) figure.append(createElement("p", "chart-question", question));
      if (!chart || !validPngDataUri(chart.data_uri)) { figure.append(createElement("p", "chart-error", "PNG 데이터가 올바르지 않아 그림을 표시하지 않았습니다.")); } else {
        const image = document.createElement("img");
        image.src = chart.data_uri;
        image.alt = typeof chart.alt === "string" && chart.alt ? chart.alt : `${title.textContent} 연구 그림`;
        image.loading = "lazy";
        image.decoding = "async";
        figure.append(image);
        if (typeof chart.caption === "string" && chart.caption) {
          figure.append(createElement("p", "chart-caption", chart.caption));
        }
        const download = createElement("a", "chart-download", "PNG 원본 저장");
        download.href = chart.data_uri;
        download.download = `${result.mode}-${String(chart.id).replace(/[^a-z0-9_-]/gi, "_")}.png`;
        figure.append(download);
      }
      container.append(figure);
    };
    for (const chart of primaryCharts) appendChart(chart, ui.charts);
    for (const chart of diagnosticCharts) appendChart(chart, ui.diagnosticCharts);
    for (const chart of observationCharts) appendChart(chart, ui.observationCharts);
    ui.chartPanel.hidden = false;
    ui.diagnosticChartsPanel.hidden = diagnosticCharts.length === 0;
  }

  function renderObservations(result) {
    const observations = result && typeof result.observations === "object" ? result.observations : null;
    if (!observations) return;
    const percent = (value) => (typeof value === "number" && Number.isFinite(value)
      ? `${Math.round(value * 100)}%` : null);
    const minutes = (value) => (typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) : null);
    const regularity = Array.isArray(observations.regularity) ? observations.regularity : [];
    renderTable(
      ui.observationRegularity,
      [
        {label: "참가자", value: "participant_id"},
        {label: "관측 밤", value: "labeled_nights"},
        {label: "SRI", value: (row) => metricValue(row.sri, 1)},
        {label: "IS", value: (row) => metricValue(row.interdaily_stability, 3)},
        {label: "IV", value: (row) => metricValue(row.intradaily_variability, 3)},
        {label: "평균 입면", value: "onset_mean_clock"},
        {label: "평균 기상", value: "wake_mean_clock"},
        {label: "중앙시각 SD(분)", value: (row) => minutes(row.midpoint_sd_min)},
        {label: "수면시간 평균(분)", value: (row) => minutes(row.duration_mean_min)},
        {label: "주말−주중 중앙시각(분)", value: (row) => minutes(row.weekend_weekday_midpoint_gap_min)},
        {label: "계산 불가 사유", value: (row) => (Array.isArray(row.unavailable) ? row.unavailable.join(" / ") : null)},
      ],
      regularity,
      "규칙성을 계산할 관측 밤이 없습니다.",
    );
    const completeness = Array.isArray(observations.completeness) ? observations.completeness : [];
    renderTable(
      ui.observationCompleteness,
      [
        {label: "참가자", value: "participant_id"},
        {label: "기간", value: (row) => `${row.first_date} ~ ${row.last_date} (${row.span_days}일)`},
        {label: "관측 비율", value: (row) => percent(row.labeled_coverage)},
        {label: "20시 전 입력", value: (row) => `${row.timely_nights}밤 (${percent(row.timely_share_of_labeled) ?? "—"})`},
        {label: "늦은 입력", value: "late_nights"},
        {label: "예측만", value: "prediction_only_nights"},
        {label: "행 없음", value: "missing_nights"},
        {label: "입력 지연 중앙값(분)", value: (row) => minutes(row.report_delay_median_min)},
        {label: "입력 지연 90%(분)", value: (row) => minutes(row.report_delay_p90_min)},
      ],
      completeness,
      "완결성을 계산할 행이 없습니다.",
    );
    const assumptions = observations.assumptions && typeof observations.assumptions === "object"
      ? Object.values(observations.assumptions) : [];
    for (const text of assumptions) {
      if (typeof text === "string") ui.observationAssumptions.append(createElement("li", "", text));
    }
    ui.observationPanel.hidden = false;
  }

  function optionValues(select, values, preferred) {
    select.replaceChildren();
    for (const value of values) {
      const option = createElement("option", "", value);
      option.value = value;
      select.append(option);
    }
    if (preferred && values.includes(preferred)) select.value = preferred;
  }

  function updatePersonalizationCutoffs(preferred) {
    const participant = ui.personalizationParticipant.value;
    const cutoffs = [...new Set(
      state.personalizationRows
        .filter((row) => String(row.participant_id) === participant)
        .map((row) => String(row.cutoff)),
    )].sort();
    optionValues(ui.personalizationCutoff, cutoffs, preferred && cutoffs.includes(preferred) ? preferred : cutoffs[cutoffs.length - 1]);
    renderPersonalizationTable();
  }

  function renderPersonalizationTable() {
    const participant = ui.personalizationParticipant.value;
    const cutoff = ui.personalizationCutoff.value;
    const snapshots = state.personalizationRows.filter(
      (row) => String(row.participant_id) === participant && String(row.cutoff) === cutoff,
    );
    const rows = [];
    for (const snapshot of snapshots) {
      rows.push({cohort: snapshot.cohort, category: "실행 정보", key: "history_count", value: displayValue(snapshot.history_count)});
      const modelState = snapshot.state && typeof snapshot.state === "object" ? snapshot.state : {};
      for (const [key, value] of Object.entries(modelState)) {
        rows.push({cohort: snapshot.cohort, category: "night_core state", key, value: displayValue(value)});
      }
      const features = snapshot.features && typeof snapshot.features === "object" ? snapshot.features : {};
      for (const [key, value] of Object.entries(features)) {
        rows.push({cohort: snapshot.cohort, category: "robust feature", key, value: displayValue(value)});
      }
    }
    renderTable(
      ui.personalization,
      [
        {label: "코호트", value: "cohort"},
        {label: "분류", value: "category"},
        {label: "입력·상태 키", value: "key"},
        {label: "실제 값", value: "value"},
      ],
      rows,
      "선택한 참가자와 cutoff의 개인화 스냅샷이 없습니다.",
    );
  }

  function renderPersonalization(result) {
    state.personalizationRows = Array.isArray(result.personalization) ? result.personalization : [];
    if (state.personalizationRows.length === 0) {
      ui.personalizationPanel.hidden = true;
      return;
    }
    const participants = [...new Set(state.personalizationRows.map((row) => String(row.participant_id)))].sort();
    optionValues(ui.personalizationParticipant, participants, participants[0]);
    updatePersonalizationCutoffs();
    ui.personalizationPanel.hidden = false;
  }

  function renderWarnings(result) {
    // The interpretation-warning panel was removed; warnings stay in the result JSON.
    if (!ui.warningPanel || !ui.warnings) return;
    ui.warnings.replaceChildren();
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const staticPrefixes = [
      "Privacy:",
      "Research comparison only:",
      "Frozen models do not use ",
      "Evidence scope:",
    ];
    const visibleWarnings = warnings
      .filter((warning) => typeof warning !== "string"
        || !staticPrefixes.some((prefix) => warning.startsWith(prefix)))
      .map((warning) => warning === "Prediction-only rows have unavailable outcomes and are excluded from error metrics; unavailable values are never treated as zero."
        ? "정답이 없는 예측 전용 행은 오차 지표에서 제외했으며, 결측값을 0으로 대체하지 않았습니다."
        : warning);
    if (visibleWarnings.length === 0) {
      ui.warningPanel.hidden = true;
      return;
    } else {
      for (const warning of visibleWarnings) ui.warnings.append(createElement("li", "", displayValue(warning)));
    }
    ui.warningPanel.hidden = false;
  }

  function renderResult(result, payload) {
    if (result.schema_version !== 1) throw new Error("지원하지 않는 평가 응답 버전입니다.");
    clearResultDisplay();
    renderSummary(result);
    renderContext(result.context || result.input_context || []);
    renderContextAnalysis(result.context_summary, result.context_features);
    renderModels(result);
    renderMetrics(result);
    renderCharts(result);
    renderObservations(result);
    renderPersonalization(result);
    renderWarnings(result);

    const modeDetail = result.mode === "synthetic"
      ? "독립 생성한 가상 설명 결과이며 실제 정확도나 임상 효과의 증거가 아닙니다."
      : "사용자 제공 자료의 후향 재생 결과이며 전향적 성능을 입증하지 않습니다.";
    const evidenceLabel = typeof result.evidence_label === "string" && result.evidence_label
      ? result.evidence_label
      : "근거 라벨 없음";
    setEvidence(evidenceLabel, modeDetail, result.mode === "synthetic" ? "synthetic" : "real");
    const runId = typeof result.run_id === "string" ? result.run_id : "식별자 없음";
    const shortRunId = runId === "식별자 없음" ? runId : runId.slice(0, 8);
    ui.currentResultState.textContent = `현재 입력의 실행 ${shortRunId} · 모든 표와 그림은 이 응답에 연결되어 있습니다.`;
    if (runId === "식별자 없음") ui.currentResultState.removeAttribute("title");
    else ui.currentResultState.title = `전체 실행 ID: ${runId}`;
    setRunStatus("현재 입력 평가를 완료했습니다.");

    const chosen = payload.chart_participant_id;
    if (chosen && [...ui.chartParticipant.options].some((option) => option.value === chosen)) {
      ui.chartParticipant.value = chosen;
    }
  }

  function renderModelStatus(status) {
    const rows = [];
    const cohorts = status && status.cohorts && typeof status.cohorts === "object" ? status.cohorts : {};
    for (const [name, detailValue] of Object.entries(cohorts)) {
      const detail = detailValue && typeof detailValue === "object" ? detailValue : {};
      rows.push({
        cohort: name,
        loaded: detail.loaded === true ? "사용 가능" : "사용 불가",
        version: detail.version || detail.model_version || detail.schema_version,
        hash: detail.sha256,
        error: detail.error,
      });
    }
    renderTable(
      ui.modelStatus,
      [
        {label: "코호트", value: "cohort"},
        {label: "상태", value: "loaded"},
        {label: "버전", value: "version"},
        {label: "SHA-256", value: "hash"},
        {label: "오류", value: "error"},
      ],
      rows,
      "코호트 모델 상태 정보가 없습니다.",
    );
    if (status && typeof status.models_dir === "string") {
      const path = createElement("p", "model-path", `로컬 모델 디렉터리: ${status.models_dir}`);
      ui.modelStatus.prepend(path);
    }
    if (status && status.available === false) {
      ui.modelStatus.prepend(createElement("p", "inline-alert", "필요한 로컬 모델을 모두 사용할 수 없습니다. 오류 열을 확인하세요."));
    }
  }

  async function loadSession() {
    try {
      const response = await fetch("/api/lab/session", {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
        headers: {"Accept": "application/json"},
      });
      const session = await responseJSON(response);
      if (typeof session.csrf_token !== "string" || !session.csrf_token) throw new Error("세션 토큰이 없습니다.");
      state.token = session.csrf_token;
      if (session.limits && Number.isInteger(session.limits.max_body_bytes)) {
        state.maxBodyBytes = session.limits.max_body_bytes;
      }
      renderModelStatus(session.model_status || {});
      state.integration = session.integration && typeof session.integration === "object" ? session.integration : null;
      const integration = state.integration || {};
      const connected = integration.connected === true;
      ui.integrationState.textContent = connected ? "본 앱 기록 연결됨 · 읽기 전용" : "본 앱 기록 연결 안 됨";
      ui.integrationState.className = `integration-state ${connected ? "connected" : "disconnected"}`;
      ui.importAppRecords.disabled = !connected;
      ui.integrationSource.replaceChildren();
      const sources = Array.isArray(integration.sources) ? integration.sources : [];
      for (const source of sources) {
        if (!source || typeof source.id !== "string") continue;
        const option = createElement("option", "", source.label || source.id);
        option.value = source.id;
        ui.integrationSource.append(option);
      }
      ui.integrationSource.disabled = sources.length < 2;
      const appUrl = safeLoopbackUrl(integration.app_url);
      if (appUrl) {
        for (const link of document.querySelectorAll("[data-app-return]")) link.href = appUrl;
      }
      if (integration.source_coverage && typeof integration.source_coverage === "object") {
        const coverage = integration.source_coverage;
        ui.importSummary.hidden = false;
        ui.importSummary.textContent = `연동 범위: ${displayValue(coverage.records ?? coverage.n_records ?? "—")}건 기록 · ${displayValue(coverage.nights ?? "—")}개 야간${coverage.note ? ` · ${coverage.note}` : ""}`;
      }
      ui.runButton.disabled = false;
      ui.sessionState.textContent = "로컬 세션 연결됨 · 저장 없음";
      setRunStatus("입력을 준비한 뒤 현재 입력 평가를 누르세요.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "세션을 시작하지 못했습니다.";
      ui.sessionState.textContent = "로컬 세션 연결 실패";
      setInputError(message);
      setEvidence("세션 오류 — 평가 안 됨", message, "error");
      setRunStatus("서버를 다시 실행하고 페이지를 새로고침해주세요.");
    }
  }

  function safeLoopbackUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const parsed = new URL(value, window.location.href);
      if (parsed.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(parsed.hostname) || parsed.username || parsed.password || (parsed.pathname !== "/" && parsed.pathname !== "")) return null;
      return parsed.href;
    } catch (_error) { return null; }
  }

  async function importAppRecords() {
    if (!state.token || !state.integration?.connected) return;
    ui.importAppRecords.disabled = true;
    ui.importSummary.hidden = false;
    ui.importSummary.textContent = "본 앱에서 읽기 전용 스냅샷을 준비하고 있습니다…";
    try {
      const response = await fetch("/api/lab/import", {
        method: "POST", cache: "no-store", credentials: "same-origin",
        headers: {"Accept": "application/json", "Content-Type": "application/json", "X-SleepWell-Token": state.token},
        body: JSON.stringify({source: ui.integrationSource.value || undefined}),
      });
      const imported = await responseJSON(response);
      if (!imported.payload || !Array.isArray(imported.payload.records)) throw new Error("연동 응답에 canonical records가 없습니다.");
      const snapshot = {payload: imported.payload, summary: imported.summary || {}, warnings: imported.warnings || [], context_provenance: imported.context_provenance || []}
      document.querySelector("input[name='mode'][value='real']").checked = true;
      document.querySelector("input[name='real-format'][value='json']").checked = true;
      ui.recordsJson.value = JSON.stringify(snapshot, null, 2);
      syncInputPanels();
      payloadChanged();
      const summary = imported.summary || {};
      const warningCount = Array.isArray(imported.warnings) ? imported.warnings.length : 0;
      const coverage = summary.coverage && typeof summary.coverage === "object" ? Object.entries(summary.coverage).map(([key, value]) => `${key} ${value}건`).join(" · ") : "";
      ui.importSummary.textContent = `읽기 전용 스냅샷 ${displayValue(summary.records ?? imported.payload.records.length)}건을 불러왔습니다${summary.context_events ? ` · 맥락 ${summary.context_events}건` : ""}${coverage ? ` · ${coverage}` : ""}${warningCount ? ` · 확인할 경고 ${warningCount}건` : ""}. 평가 시 고정 모델의 조건별 진단으로만 사용됩니다.`;
      if (warningCount) {
        ui.importSummary.append(document.createElement("br"));
        ui.importSummary.append(createElement("span", "source-note", imported.warnings.map(displayValue).join(" · ")));
      }
      setRunStatus("연동 기록을 입력란에 넣었습니다. 모델 평가 전 내용을 확인하세요.");
    } catch (error) {
      ui.importSummary.textContent = error instanceof Error ? error.message : "본 앱 기록을 불러오지 못했습니다.";
      setInputError(ui.importSummary.textContent);
    } finally {
      ui.importAppRecords.disabled = !state.integration?.connected;
    }
  }

  function downloadBlob(contents, contentType, filename) {
    const blob = new Blob([contents], {type: contentType});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async function downloadPublicationBundle() {
    if (!state.lastPayload || !state.lastResult || state.inFlight || state.exporting || !state.token) return;
    const exportRevision = state.revision;
    const exportPayload = cloneJSON(state.lastPayload);
    const rawId = typeof state.lastResult.run_id === "string" ? state.lastResult.run_id : "run";
    const safeId = rawId.replace(/[^A-Za-z0-9._-]/g, "_");
    state.exporting = true;
    ui.chartParticipant.disabled = true;
    ui.downloadPublication.disabled = true;
    setInputError("");
    setRunStatus("논문용 벡터 그림·PNG·원자료 묶음을 생성하고 있습니다.");
    try {
      const response = await fetch("/api/lab/export", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          "Accept": "application/zip, application/json",
          "Content-Type": "application/json",
          "X-SleepWell-Token": state.token,
        },
        body: JSON.stringify(exportPayload),
      });
      const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
      if (!response.ok || contentType.includes("application/json")) {
        let message = `논문용 묶음 생성이 실패했습니다(HTTP ${response.status}).`;
        try {
          const data = await response.json();
          if (data && typeof data.error === "string") message = data.error;
        } catch (_error) { /* use bounded status message */ }
        throw new Error(message);
      }
      if (!contentType.includes("application/zip")) throw new Error("서버가 ZIP 형식이 아닌 응답을 보냈습니다.");
      const blob = await response.blob();
      if (!blob.size) throw new Error("생성된 논문용 묶음이 비어 있습니다.");
      const payloadStillCurrent = state.lastPayload
        && JSON.stringify(state.lastPayload) === JSON.stringify(exportPayload);
      if (state.revision !== exportRevision || state.inFlight || !state.lastResult || !payloadStillCurrent) {
        setRunStatus("내보내는 동안 입력이 변경되어 이전 입력의 묶음을 저장하지 않았습니다.");
        return;
      }
      downloadBlob(blob, "application/zip", `sleepwell-publication-${safeId}.zip`);
      setRunStatus("논문용 그림·데이터 묶음을 저장했습니다.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "논문용 묶음을 생성하지 못했습니다.";
      setInputError(message);
      setRunStatus("현재 평가 결과는 유지됩니다. 내보내기를 다시 시도해주세요.");
    } finally {
      state.exporting = false;
      ui.chartParticipant.disabled = !state.lastPayload || state.inFlight;
      ui.downloadPublication.disabled = !state.lastPayload || state.inFlight;
    }
  }

  for (const control of document.querySelectorAll("[data-payload]")) {
    const eventName = control.matches("textarea, input[type='number'], input[type='date']") ? "input" : "change";
    control.addEventListener(eventName, () => {
      syncInputPanels();
      payloadChanged();
    });
  }

  ui.autoRun.addEventListener("change", () => {
    window.clearTimeout(state.autoTimer);
    if (ui.autoRun.checked) scheduleAutomaticRun();
  });

  ui.runButton.addEventListener("click", () => requestFormEvaluation("명시적 평가"));
  ui.importAppRecords.addEventListener("click", () => { void importAppRecords(); });

  ui.csvFile.addEventListener("change", async () => {
    const file = ui.csvFile.files && ui.csvFile.files[0];
    if (!file) return;
    if (file.size > state.maxBodyBytes) {
      setInputError("CSV 파일은 2 MiB 이하여야 합니다.");
      ui.csvFile.value = "";
      return;
    }
    try {
      ui.csvText.value = await file.text();
      const csvRadio = document.querySelector("input[name='real-format'][value='csv']");
      csvRadio.checked = true;
      syncInputPanels();
      payloadChanged();
    } catch (_error) {
      setInputError("CSV 파일을 브라우저에서 읽지 못했습니다.");
    }
  });

  ui.downloadTemplate.addEventListener("click", () => {
    const header = "participant_id,cutoff,sleep_start,sleep_end,observed_at\r\n";
    downloadBlob(header, "text/csv;charset=utf-8", "sleepwell-research-lab-template.csv");
  });

  ui.downloadResult.addEventListener("click", () => {
    if (!state.lastResult) return;
    const rawId = typeof state.lastResult.run_id === "string" ? state.lastResult.run_id : "run";
    const safeId = rawId.replace(/[^A-Za-z0-9._-]/g, "_");
    downloadBlob(
      `${JSON.stringify(state.lastResult, null, 2)}\n`,
      "application/json;charset=utf-8",
      `sleepwell-research-lab-${safeId}.json`,
    );
  });
  ui.downloadPublication.addEventListener("click", () => { void downloadPublicationBundle(); });

  ui.chartParticipant.addEventListener("change", () => {
    if (state.exporting || !state.lastPayload || !ui.chartParticipant.value) return;
    const payload = cloneJSON(state.lastPayload);
    if (payload.chart_participant_id === ui.chartParticipant.value) return;
    payload.chart_participant_id = ui.chartParticipant.value;
    enqueueEvaluation({payload, revision: state.revision, reason: "그림 참가자 재평가"});
  });

  ui.personalizationParticipant.addEventListener("change", () => updatePersonalizationCutoffs());
  ui.personalizationCutoff.addEventListener("change", renderPersonalizationTable);

  // Embedded in the researcher shell: #embed=1&theme=dark&source=<participant>.
  function applyEmbedState() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    document.documentElement.classList.toggle("embedded", params.get("embed") === "1");
    const theme = params.get("theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    return params;
  }

  async function importFromHash() {
    const source = applyEmbedState().get("source");
    if (!source || !state.integration?.connected) return;
    if (![...ui.integrationSource.options].some((option) => option.value === source)) return;
    ui.integrationSource.value = source;
    await importAppRecords();
  }

  window.addEventListener("hashchange", () => { void importFromHash(); });
  applyEmbedState();
  syncInputPanels();
  void loadSession().then(importFromHash);
})();
