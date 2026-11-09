/* Read-only scientific diagnostics. All displayed values come from the local API. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const SVG = 'http://www.w3.org/2000/svg';
  const labels = {
    cmi: 'CMI', mesa: 'MESA', physical: '물리 모델', data: '데이터 모델', hybrid: '융합 모델',
    joint_mae_min: '공동 MAE', onset_mae_min: '수면 시작 MAE', wake_mae_min: '기상 MAE',
    interval_mae_min: '수면 간격 MAE', n_people: '평가 인원', n_nights: '평가 밤',
    bed: '예상 수면 시작', wake: '예상 기상', interval_min: '예상 수면 간격',
    s: '수면압력 S', c: '일주기 C', threshold: '임계값', fallback: '대체 경로',
    valid_days: '유효 기록일', personal_weight: '개인 보정 비중', personalization_weight: '개인화 비중',
    effective_days: '감쇠 유효일', recent_effective_days: '최근 감쇠 유효일', history_count: '기록 수'
  };
  Object.assign(labels, {
    population: 'TRAIN 집단 prior (가용 이력 미사용)',
    last: '최근 28일 창의 마지막 기록',
    mean7: '최근 7일 창 평균',
    mean14: '최근 14일 창 평균',
    weighted28: '최근 28일 창 가중평균 (반감기 14일)',
    weekday: '최근 28일 같은 요일 평균 (2개 미만이면 14일 평균)',
    legacy_data: '기존 데이터 모델', legacy_hybrid: '기존 융합 모델', legacy_physical: '기존 물리 모델'
  });
  const modelOrder = ['physical', 'data', 'hybrid'];
  let consoleData = null;

  function present(value) { return value !== null && value !== undefined && value !== ''; }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function text(value, fallback = '자료 없음') { return present(value) ? String(value) : fallback; }
  function number(value, suffix = '', digits = 1) {
    return finite(value) ? `${Number(value).toFixed(digits)}${suffix}` : '자료 없음';
  }
  function localizedTime(value) {
    if (!present(value)) return '자료 없음';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('ko-KR');
  }
  function clear(node) { if (node) node.replaceChildren(); }
  function el(tag, content, className) {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    if (className) node.className = className;
    return node;
  }
  function addDl(dl, key, value) {
    dl.append(el('dt', key), el('dd', text(value)));
  }
  function statusText(value) {
    const map = {ready: '연결됨', available: '사용 가능', research_comparison_only: '연구 비교용 · 연결됨', incomplete: '일부 연결', missing: '자료 없음', partial: '일부 연결', empty: '수신 자료 없음', no_snapshots: '수신 자료 없음'};
    return map[value] || text(value, '상태 미표시');
  }
  function shortHash(value) { return present(value) ? `${String(value).slice(0, 12)}…` : '자료 없음'; }
  function setError(id, message) {
    const target = byId(id);
    if (!target) return;
    target.textContent = message || '';
    target.hidden = !message;
  }
  function makeTable(headers, rows) {
    const table = document.createElement('table');
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    headers.forEach(value => headRow.append(el('th', value)));
    head.append(headRow); table.append(head);
    const body = document.createElement('tbody');
    if (!rows.length) {
      const cell = el('td', '표시할 자료가 없습니다.', 'value-missing');
      cell.colSpan = headers.length; const row = document.createElement('tr'); row.append(cell); body.append(row);
    } else {
      rows.forEach(values => { const row = document.createElement('tr'); values.forEach(value => row.append(el('td', value))); body.append(row); });
    }
    table.append(body); return table;
  }
  function scalarRows(value, prefix = '', depth = 0) {
    if (depth > 4 || !present(value)) return [];
    if (Array.isArray(value)) return value.flatMap((item, index) => scalarRows(item, `${prefix}[${index}]`, depth + 1));
    if (typeof value === 'object') return Object.entries(value).flatMap(([key, item]) => scalarRows(item, prefix ? `${prefix}.${key}` : key, depth + 1));
    return [[labels[prefix] || prefix.replaceAll('_', ' '), typeof value === 'boolean' ? (value ? '예' : '아니오') : String(value)]];
  }
  function findNumeric(value, keys, depth = 0) {
    if (depth > 5 || !value || typeof value !== 'object') return null;
    for (const [key, item] of Object.entries(value)) {
      if (keys.includes(key) && finite(item)) return item;
    }
    for (const item of Object.values(value)) {
      const found = findNumeric(item, keys, depth + 1);
      if (found !== null) return found;
    }
    return null;
  }

  function renderChart(metrics) {
    const target = byId('cohort-chart'); clear(target);
    const series = modelOrder.flatMap(model => ['joint_mae_min']
      .filter(metric => finite(metrics?.[model]?.[metric])).map(metric => ({model, metric, value: Number(metrics[model][metric])})));
    if (!series.length) { target.append(el('p', '그래프로 표시할 오차 수치가 없습니다.', 'value-missing')); return; }
    const width = 720, left = 142, right = 68, top = 20, row = 28, height = top + series.length * row + 34;
    const max = Math.max(...series.map(item => item.value), 1);
    const svg = document.createElementNS(SVG, 'svg'); svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('aria-hidden', 'true');
    for (let i = 0; i <= 4; i += 1) {
      const x = left + (width - left - right) * i / 4;
      const line = document.createElementNS(SVG, 'line'); line.setAttribute('x1', x); line.setAttribute('x2', x); line.setAttribute('y1', top - 5); line.setAttribute('y2', height - 25); line.setAttribute('class', 'chart-grid'); svg.append(line);
      const tick = document.createElementNS(SVG, 'text'); tick.setAttribute('x', x); tick.setAttribute('y', height - 6); tick.setAttribute('text-anchor', 'middle'); tick.setAttribute('class', 'chart-label'); tick.textContent = `${(max * i / 4).toFixed(0)}분`; svg.append(tick);
    }
    series.forEach((item, index) => {
      const y = top + index * row;
      const label = document.createElementNS(SVG, 'text'); label.setAttribute('x', left - 10); label.setAttribute('y', y + 15); label.setAttribute('text-anchor', 'end'); label.setAttribute('class', 'chart-label'); label.textContent = `${labels[item.model]} · ${labels[item.metric]}`; svg.append(label);
      const bar = document.createElementNS(SVG, 'rect'); bar.setAttribute('x', left); bar.setAttribute('y', y + 3); bar.setAttribute('width', Math.max(1, (width - left - right) * item.value / max)); bar.setAttribute('height', 16); bar.setAttribute('rx', 3); bar.setAttribute('class', `chart-bar-${item.model}`); svg.append(bar);
      const value = document.createElementNS(SVG, 'text'); value.setAttribute('x', Math.min(width - 2, left + (width - left - right) * item.value / max + 6)); value.setAttribute('y', y + 15); value.setAttribute('class', 'chart-value'); value.textContent = `${item.value.toFixed(1)}`; svg.append(value);
    });
    target.append(svg);
  }

  function renderCohort() {
    const selector = byId('cohort-selector');
    const cohort = consoleData?.research?.cohorts?.find(item => item.cohort === selector.value) || consoleData?.research?.cohorts?.[0];
    if (!cohort) {
      byId('cohort-summary').textContent = '연결된 연구 코호트가 없습니다.';
      byId('cohort-metric-rows').replaceChildren(el('tr'));
      byId('cohort-metric-rows').firstChild.append(Object.assign(el('td', '모델 산출물을 찾지 못했습니다.', 'value-missing'), {colSpan: 7}));
      clear(byId('cohort-chart')); clear(byId('cohort-connection')); clear(byId('cohort-provenance')); return;
    }
    const metrics = cohort.test_metrics || {};
    const targetLabel = cohort.target === 'next main-sleep onset and final wake offsets in minutes after the local 20:00 cutoff'
      ? '다음 주수면 입면·최종 기상 시각' : text(cohort.target);
    byId('cohort-summary').textContent = `${labels[cohort.cohort] || cohort.cohort} · ${targetLabel} · 모델 ${text(cohort.model_version)} · ${statusText(cohort.status)}`;
    const rows = modelOrder.map(model => {
      const item = metrics[model] || {};
      return [labels[model], number(item.onset_mae_min, '분'), number(item.wake_mae_min, '분'), number(item.joint_mae_min, '분'), number(item.interval_mae_min, '분'), number(item.n_people, '명', 0), number(item.n_nights, '밤', 0)];
    });
    const body = byId('cohort-metric-rows'); clear(body);
    rows.forEach(values => { const row = document.createElement('tr'); values.forEach(value => row.append(el('td', value))); body.append(row); });
    const sample = modelOrder.map(model => metrics[model]).find(item => finite(item?.n_people) || finite(item?.n_nights));
    byId('cohort-sample').textContent = sample ? `테스트 표본 ${number(sample.n_people, '명', 0)} · ${number(sample.n_nights, '밤', 0)}` : '테스트 표본 수 미표시';
    renderChart(metrics);
    const connection = byId('cohort-connection'); clear(connection);
    const methodology = consoleData?.research?.methodology || {};
    const physical = methodology.physical_model || {};
    addDl(connection, '연결 상태', statusText(cohort.status)); addDl(connection, '의사결정 시각', cohort.decision_time);
    const averaging = metrics.hybrid?.averaging || metrics.data?.averaging;
    addDl(connection, '물리 계산 제공자', cohort.physical_provider === 'two_process_fixed_v1' ? 'Two-Process 고정 상수' : cohort.physical_provider);
    addDl(connection, '평가 방식', averaging === 'equal participant' ? '참여자별 동일 가중' : averaging || '저장된 테스트 분할');
    const fixed = physical.fixed_parameters || {};
    addDl(connection, '고정 물리 상수', `τ 각성 ${text(fixed.tau_wake_h)} h · τ 수면 ${text(fixed.tau_sleep_h)} h · 일주기 진폭 ${text(fixed.circadian_amplitude)} · 계산 간격 ${text(fixed.simulation_step_min)}분`);
    const population = consoleData?.research?.population_prior || {};
    addDl(connection, '추천용 초기 집단값 (연구 예측과 별도)', population.available ? `${text(population.source)} · ${text(population.group_count)}개 집단 · 집단당 최소 ${text(population.minimum_group_people)}명` : population.reason);
    byId('cohort-caveat').textContent = `${text(cohort.test_caveat)} ${Array.isArray(cohort.limitations) ? cohort.limitations.join(' · ') : ''}`.trim();
    const provenance = byId('cohort-provenance'); clear(provenance);
    addDl(provenance, '연구 집단', cohort.provenance?.study_cohort); addDl(provenance, '적합 실행', cohort.provenance?.fitted_run);
    addDl(provenance, '시드', cohort.provenance?.seed); addDl(provenance, '학습 인원 / 행', `${text(cohort.provenance?.training_people)} / ${text(cohort.provenance?.training_rows)}`);
    addDl(provenance, '원본 산출물 SHA-256', shortHash(cohort.provenance?.source_artifact_sha256)); addDl(provenance, '특성 SHA-256', shortHash(cohort.provenance?.source_features_sha256));
    addDl(provenance, '전처리', cohort.preprocessing?.processing); addDl(provenance, '선택 규칙', cohort.preprocessing?.selection);
    addDl(provenance, '집단 prior', cohort.prior ? `${text(cohort.prior.method)} · ${text(cohort.prior.n_people)}명 / ${text(cohort.prior.n_nights)}밤` : null);
    addDl(provenance, '집단 prior 역할', '선택한 연구 코호트의 Train 밤에서 계산한 모델 행동 기준입니다. NHANES 초기 추천 prior와 별개입니다.');
    const populationGlobal = population.global || {};
    addDl(provenance, 'NHANES 초기 추천 prior', population.available ? `${text(population.source)} · 전체 ${text(populationGlobal.n_people || populationGlobal.people || populationGlobal.n)}명 · ${text(population.interpretation)}` : population.reason);
    Object.entries(physical.equations || {}).forEach(([key, value]) => addDl(provenance, `물리식 · ${key}`, value));
    const frozen = methodology.frozen_model_personalization || {};
    addDl(provenance, '동결 모델 개인화 조건', frozen.activation); addDl(provenance, '동결 모델 개인화 가중식', frozen.weight);
    addDl(provenance, '동결 모델 행동 기준식', frozen.habit); addDl(provenance, '개인화 해석', frozen.note);
    addDl(provenance, '사용자 추천 갱신', '첫 유효 일지부터 14일 반감기와 초기값 수축으로 매일 갱신합니다. 동결 연구 모델 계약과 별개입니다.');
    addDl(provenance, '온라인 잔차 보정', '첫 확정 결과부터 별도 연구 비교값으로 계산하며, 원 연구 예측을 덮어쓰지 않습니다.');
    addDl(provenance, '검증 해석', '기존 Validation/Test를 재사용한 적응형 분석이며 새 외부 검증이 아닙니다.');
  }

  function populateCohorts() {
    const selector = byId('cohort-selector'); const previous = selector.value; clear(selector);
    const cohorts = consoleData?.research?.cohorts || [];
    cohorts.forEach(item => { const option = el('option', labels[item.cohort] || item.cohort); option.value = item.cohort; selector.append(option); });
    if ([...selector.options].some(option => option.value === previous)) selector.value = previous;
    selector.disabled = !cohorts.length; renderCohort();
  }

  function participantLabel(item) {
    const key = text(item.participant_key, '키 없음');
    return `${key.slice(0, 8)}… · 기기 ${text(item.device_id).slice(0, 8)}… · 최근 ${text(item.latest_day)}`;
  }
  function currentParticipant() {
    return consoleData?.phone?.participants?.find(item => `${item.device_id}|${item.participant_key}` === byId('participant-selector').value);
  }
  function currentSnapshot() {
    const participant = currentParticipant();
    return participant?.days?.find(item => `${item.day}|${item.revision}` === byId('snapshot-selector').value) || participant?.days?.[0];
  }
  function fillDlFromRows(target, rows, emptyText) {
    clear(target);
    if (!rows.length) { addDl(target, '상태', emptyText); return; }
    rows.slice(0, 40).forEach(([key, value]) => addDl(target, key, value));
  }
  function researchModelLabel(key) {
    const match = /^(physical|data|hybrid)_(cmi|mesa)$/.exec(key || '');
    return match ? `${labels[match[1]]} · ${labels[match[2]]}` : text(key);
  }
  function booleanText(value) { return typeof value === 'boolean' ? (value ? '예' : '아니오') : '자료 없음'; }
  function firstMetric(container, keys) {
    if (!container || typeof container !== 'object') return null;
    for (const key of keys) if (finite(container[key])) return container[key];
    return null;
  }
  function validationMetrics(item, side) {
    const value = item?.[side] || {};
    return {
      onset: firstMetric(value, ['onset_mae_min', 'onset']),
      wake: firstMetric(value, ['wake_mae_min', 'wake']),
      joint: firstMetric(value, ['joint_mae_min', 'joint'])
    };
  }
  function renderPersonalizationValidation() {
    const report = consoleData?.research?.personalization_validation || {};
    const status = byId('personalization-validation-status');
    const note = byId('personalization-validation-note');
    const summary = byId('personalization-validation-summary');
    const strata = byId('personalization-validation-strata');
    const detail = byId('personalization-validation-detail');
    clear(summary); clear(strata); clear(detail);
    if (!report.available) {
      status.textContent = '검증 산출물 없음';
      note.textContent = text(report.interpretation || report.reason);
      summary.append(el('p', '실제 aggregate 검증 파일이 연결될 때만 수치를 표시합니다.', 'value-missing'));
      strata.append(el('p', '표시할 기록 축적 구간 결과가 없습니다.', 'value-missing'));
      return;
    }
    status.textContent = report.evaluation_status?.fresh_external_confirmation ? '새 외부 검증' : '재사용 시험자료 · 탐색적';
    note.textContent = `${text(report.interpretation)} · 오차 감소는 양수일 때만 추가 보정이 개선된 것입니다. · 추정 대상: ${text(report.estimand)} · ${text(report.target)}`;
    const summaryRows = (report.cohorts || []).map(item => {
      const base = validationMetrics(item, 'base_metrics');
      const corrected = validationMetrics(item, 'corrected_metrics');
      const improvement = item.paired_improvement?.joint || {};
      const ci = Array.isArray(improvement.ci95) && improvement.ci95.length === 2
        ? `[${number(improvement.ci95[0], '', 2)}, ${number(improvement.ci95[1], '', 2)}]분` : '자료 없음';
      return [labels[item.cohort] || text(item.cohort), labels[item.base_arm] || text(item.base_arm), number(item.n_people, '명', 0),
        number(item.n_nights, '밤', 0), number(item.eligible_personalized_nights, '밤', 0),
        number(item.coverage, '', 3), number(base.joint, '분'), number(corrected.joint, '분'),
        number(improvement.error_reduction_min, '분', 2), ci];
    });
    summary.append(makeTable(['코호트', '기준 모델', '인원', '전체 밤', '보정 가능 밤', '범위', '추가 보정 전', '추가 보정 후', '오차 감소', '95% 구간'], summaryRows));
    const stratumRows = [];
    (report.cohorts || []).forEach(item => {
      Object.entries(item.history_strata || {}).forEach(([name, value]) => {
        const base = validationMetrics(value, 'base_metrics');
        const corrected = validationMetrics(value, 'corrected_metrics');
        const diff = value?.paired_improvement?.joint || value?.joint || {};
        stratumRows.push([labels[item.cohort] || item.cohort, labels[item.base_arm] || item.base_arm, name, number(value?.n_people, '명', 0),
          number(value?.n_nights, '밤', 0), number(base.joint, '분'), number(corrected.joint, '분'),
          number(diff.error_reduction_min, '분', 2)]);
      });
    });
    strata.append(makeTable(['코호트', '기준 모델', '이전 확정 기록', '인원', '밤', '보정 전', '보정 후', '오차 감소'], stratumRows));
    const detailRows = scalarRows({policy: report.policy, evaluation_status: report.evaluation_status,
      gap_and_quality_strata: (report.cohorts || []).map(item => ({cohort: item.cohort, gap: item.gap_strata, quality: item.qc_strata})),
      limitations: report.limitations});
    detail.append(makeTable(['검증 항목', '저장 값'], detailRows));
  }
  function renderContinuousConstantsValidation() {
    const report = consoleData?.research?.continuous_constants_validation || {};
    const status = byId('continuous-constants-status');
    const note = byId('continuous-constants-note');
    const summary = byId('continuous-constants-summary');
    const detail = byId('continuous-constants-detail');
    clear(summary); clear(detail);
    if (!report.available) {
      status.textContent = '연구 산출물 없음';
      note.textContent = text(report.interpretation || report.reason);
      summary.append(el('p', '검증된 집계 산출물이 연결될 때만 수치를 표시합니다.', 'value-missing'));
      detail.append(el('p', '동결 모델이나 서비스 정책은 변경되지 않았습니다.', 'value-missing'));
      return;
    }
    status.textContent = report.test_status === 'retrospective_reused_test'
      ? '재사용 Test · 연구 전용' : text(report.test_status);
    note.textContent = `${text(report.interpretation)} · 양의 변화는 매일 갱신 정책의 MAE 감소를 뜻합니다. · 서비스 반영 없음: ${booleanText(report.no_service_promotion)}`;
    const rows = [];
    (report.cohorts || []).forEach(item => {
      ['data', 'hybrid'].forEach(arm => {
        const comparison = item.comparisons?.[arm] || {};
        const ci = Array.isArray(comparison.ci95_min) && comparison.ci95_min.length === 2
          ? `[${number(comparison.ci95_min[0], '', 2)}, ${number(comparison.ci95_min[1], '', 2)}]분` : '자료 없음';
        rows.push([labels[item.cohort] || text(item.cohort), labels[arm] || arm,
          number(item.n_people, '명', 0), number(item.n_nights, '밤', 0),
          number(item.legacy_active_nights, '밤', 0), number(item.daily_active_nights, '밤', 0),
          number(item.scores?.legacy?.[arm], '분'), number(item.scores?.daily?.[arm], '분'),
          number(comparison.gain_min, '분', 2), ci, text(item.selected_policy)]);
      });
    });
    summary.append(makeTable(['코호트', '모델', '인원', '평가 밤', '14일 활성 밤', '매일 활성 밤', '14일 MAE', '매일 MAE', 'MAE 감소', '95% 구간', '선택 정책'], rows));
    const detailRows = (report.cohorts || []).map(item => {
      const physics = item.physics_addition || {};
      const ci = Array.isArray(physics.ci95_min) && physics.ci95_min.length === 2
        ? `[${number(physics.ci95_min[0], '', 2)}, ${number(physics.ci95_min[1], '', 2)}]분` : '자료 없음';
      return [labels[item.cohort] || text(item.cohort), text(item.selected_policy),
        number(item.max_history, '건', 0), booleanText(item.validation_gate),
        number(item.validation_gain_min, '분', 2), number(item.scores?.daily?.physical, '분'),
        number(physics.gain_min, '분', 2), ci];
    });
    detail.append(makeTable(['코호트', '선택 정책', '최대 이력', '검증 게이트', '검증 변화', '물리 단독 MAE', '물리 특성 추가 효과', '95% 구간'], detailRows));
  }
  function renderTargetValidityValidation() {
    const report = consoleData?.research?.target_validity_validation || {};
    const status = byId('target-validity-status');
    const note = byId('target-validity-note');
    const summary = byId('target-validity-summary');
    const history = byId('target-validity-history');
    const detail = byId('target-validity-detail');
    clear(summary); clear(history); clear(detail);
    if (!report.available) {
      status.textContent = '검증 산출물 없음';
      note.textContent = text(report.interpretation || report.reason);
      summary.append(el('p', '엄격한 집계 계약을 통과한 결과만 표시합니다.', 'value-missing'));
      history.append(el('p', '표시할 기록 축적 구간 결과가 없습니다.', 'value-missing'));
      return;
    }
    status.textContent = '재사용 Test · 연구 전용';
    note.textContent = `${text(report.interpretation)} · MAE 이득과 Skill은 양수일 때 최근 14일 창 평균 기준선보다 좋습니다. Skill = 1 - (모델의 참여자-macro 공동 MAE / 기준선의 참여자-macro 공동 MAE)이며 개인별 Skill의 평균이 아닙니다. · 서비스 반영 없음: ${booleanText(report.no_service_promotion)}`;
    const rows = [];
    (report.cohorts || []).forEach(cohort => (cohort.models || []).forEach(model => {
      const ci = Array.isArray(model.ci95_min) && model.ci95_min.length === 2
        ? `[${number(model.ci95_min[0], '', 2)}, ${number(model.ci95_min[1], '', 2)}]분` : '자료 없음';
      rows.push([labels[cohort.cohort] || cohort.cohort, labels[model.name] || model.name,
        number(cohort.n_people, '명', 0), number(cohort.n_nights, '밤', 0),
        number(model.onset_mae_min, '분'), number(model.wake_mae_min, '분'),
        number(model.joint_mae_min, '분'), number(model.gain_vs_recent14_min, '분', 2),
        number(model.skill_vs_recent14, '', 3), ci,
        finite(model.participant_improved_fraction) ? `${(model.participant_improved_fraction * 100).toFixed(1)}%` : '자료 없음']);
    }));
    summary.append(makeTable(['코호트', '모델', '인원', '밤', '입면 MAE', '기상 MAE', '공동 MAE', '기준선 대비 이득', 'Skill', '95% 구간', '개선 참여자'], rows));
    const historyRows = [];
    (report.cohorts || []).forEach(cohort => (cohort.history_strata || []).forEach(item => {
      historyRows.push([labels[cohort.cohort] || cohort.cohort, item.label,
        number(item.n_people, '명', 0), number(item.n_nights, '밤', 0),
        number(item.baseline_mae_min, '분'), number(item.data_mae_min, '분'), number(item.hybrid_mae_min, '분')]);
    }));
    history.append(makeTable(['코호트', '이전 가용 기록 수', '인원', '밤', '최근 14일 창 기준선', '데이터 모델', '융합 모델'], historyRows));
    const detailRows = scalarRows({
      cohorts: (report.cohorts || []).map(item => ({cohort: item.cohort, label_source: item.label_source,
        availability_assumption: item.availability_assumption, feasibility: item.feasibility})),
      limitations: report.limitations
    });
    detail.append(makeTable(['검증 항목', '저장 값'], detailRows));
  }
  function renderPhoneTargetValidity(snapshot) {
    renderMeasurementAudit(snapshot);
    const report = snapshot.target_validity || {};
    const status = byId('phone-target-validity-status');
    const note = byId('phone-target-validity-note');
    const target = byId('phone-target-validity');
    const detail = byId('phone-target-validity-detail');
    clear(target); clear(detail);
    const paired = Object.values(report.agreement?.by_wearable_source || {})
      .reduce((total, item) => total + (finite(item?.paired_nights) ? item.paired_nights : 0), 0);
    status.textContent = paired ? `짝지은 밤 ${paired}건 · 기술통계` : '짝지을 측정자료 없음';
    note.textContent = '자기보고 일지를 주 기준, 착용기록을 보조 측정으로 두고 동일 밤의 기록된 취침·기상시각 차이를 기술합니다. 수동 취침시각은 침대에 들어간 시각일 수 있고 착용기록의 시작시각은 기기 알고리즘 정의이므로 서로 같은 생리학적 입면 측정이 아닙니다.';
    const rows = [];
    Object.entries(report.agreement?.by_wearable_source || {}).forEach(([source, value]) => {
      ['onset', 'wake'].forEach(point => {
        const metrics = value?.[point] || {};
        rows.push([source, point === 'onset' ? '기록된 취침시각' : '기록된 기상시각', number(value?.paired_nights, '밤', 0),
          number(metrics.median_absolute_difference_min, '분'), number(metrics.mean_signed_difference_min, '분'),
          number(metrics.proportion_absolute_over_30_min, '', 3), number(metrics.proportion_absolute_over_60_min, '', 3)]);
      });
    });
    target.append(makeTable(['착용 자료', '시점', '짝지은 밤', '절대 차이 중앙값', '부호 차이 평균', '>30분 비율', '>60분 비율'], rows));
    const detailRows = scalarRows({coverage: report.coverage, exclusions: report.exclusions,
      update_lag: report.update_lag, wear_quality: report.wear_quality, limitations: report.limitations});
    detail.append(makeTable(['감사 항목', '계산 값'], detailRows));
  }
  function renderSnapshot() {
    const snapshot = currentSnapshot();
    const analysis = byId('phone-analysis');
    if (!snapshot) { analysis.hidden = true; return; }
    analysis.hidden = false;
    renderPhoneTargetValidity(snapshot);
    byId('snapshot-summary').textContent = `${snapshot.day} · 개정 ${text(snapshot.revision)} · 생성 ${localizedTime(snapshot.generated_at)} · 수신 ${localizedTime(snapshot.received_at)}`;
    const diagnostics = snapshot.model_diagnostics || {};
    const featureReplay = diagnostics.feature_output_replay || {};
    const replay = diagnostics.replay || {};
    const outputsMatch = featureReplay.status === 'feature_outputs_match';
    const outputsMismatch = featureReplay.status === 'feature_outputs_mismatch';
    const badge = byId('snapshot-verification'); badge.classList.toggle('verified', outputsMatch);
    badge.classList.toggle('mismatch', outputsMismatch);
    badge.textContent = outputsMatch ? '입력 특성→출력 계산 일치'
      : outputsMismatch ? '입력 특성→출력 계산 불일치'
        : `휴대폰 보고값 · ${text(featureReplay.reason || snapshot.analysis_status, '출력 재계산 미실시')}`;
    const frozenForecasts = snapshot.prospective_forecasts || [];
    const frozen = frozenForecasts[frozenForecasts.length - 1];
    const recommendation = snapshot.recommendation || {};
    const exposure = diagnostics.recommendation_exposure || {};
    const comparisons = snapshot.outcome_summary?.realized_comparisons;
    byId('phone-prediction-evidence').textContent = frozen
      ? `${frozenForecasts.length}개 고정 예측 저장` : '고정된 전향 예측 없음';
    const exposedAt = exposure.exposed_at || exposure.viewed_at || recommendation.exposed_at || recommendation.viewed_at;
    byId('phone-recommendation-evidence').textContent = exposedAt
      ? `노출 확인 · ${localizedTime(exposedAt)}` : (present(recommendation.generated_at) ? '생성됨 · 노출 미확인' : '추천 없음');
    const comparisonCount = Array.isArray(comparisons) ? comparisons.length : 0;
    byId('phone-effect-evidence').textContent = comparisonCount
      ? `실제 결과 비교 ${comparisonCount}건 · 기술통계` : '독립 실제 결과 비교 없음';
    const sources = Object.entries(snapshot.record_summary?.source_counts || {}).map(([source, count]) => `${source} ${count}건`).join(' · ');
    byId('phone-record-summary').textContent = `수신 기록 ${text(snapshot.record_summary?.count)}건 · 관측 날짜 ${text(snapshot.record_summary?.observed_days)}일 / 범위 ${text(snapshot.record_summary?.calendar_span_days)}일 · 날짜 존재율 ${number(snapshot.record_summary?.observed_day_coverage, '', 3)} · 최대 공백 ${number(snapshot.record_summary?.maximum_unobserved_gap_days, '일', 0)}${sources ? ` · ${sources}` : ''} · 최근 기록일 ${text(snapshot.record_summary?.latest_date)} · 현재 모델 ${text(diagnostics.model_version)} / 계산 기준 ${localizedTime(diagnostics.cutoff)} · ${frozen ? `별도 고정 예측 ${text(frozen.night_id)} / cutoff ${localizedTime(frozen.cutoff)}` : '별도 고정 예측 없음'}`;
    const outputOrder = ['physical_cmi', 'data_cmi', 'hybrid_cmi', 'physical_mesa', 'data_mesa', 'hybrid_mesa'];
    const outputs = diagnostics.model_outputs || {};
    const modelRows = outputOrder.filter(key => outputs[key]).map(key => {
      const value = outputs[key];
      const onset = value?.onset_offset_min, wake = value?.wake_offset_min;
      return [researchModelLabel(key), number(onset, '분'), number(wake, '분'), finite(onset) && finite(wake) ? number(wake - onset, '분') : '자료 없음'];
    });
    const outputTarget = byId('phone-output-table'); clear(outputTarget); outputTarget.append(makeTable(['모델', '입면 (20시 이후)', '기상 (20시 이후)', '간격 계산'], modelRows));
    const sidecar = frozen?.sidecar_evidence;
    const sidecarRows = (sidecar?.arms || []).map(item => [
      researchModelLabel(item.arm), number(item.base?.onset_offset_min, '분'), number(item.corrected?.onset_offset_min, '분'),
      number(item.base?.wake_offset_min, '분'), number(item.corrected?.wake_offset_min, '분'),
      booleanText(item.active), finite(item.eligible_count) ? number(item.eligible_count, '건', 0) : '자료 없음'
    ]);
    const sidecarTarget = byId('phone-sidecar-table'); clear(sidecarTarget);
    if (sidecarRows.length) sidecarTarget.append(makeTable(['모델', '기준 입면', '보정 입면', '기준 기상', '보정 기상', '보정 활성', '이전 결과'], sidecarRows));
    else sidecarTarget.append(el('p', '유효한 추가 잔차 보정 sidecar가 없습니다.', 'value-missing'));
    byId('phone-sidecar-note').textContent = sidecar
      ? `출처 ${text(sidecar.source)} · 상태 ${text(sidecar.status)} · 정책 ${text(sidecar.policy_version)} · 기준 모델 식별자 ${text(sidecar.base_artifact_identity_sha256)} · 스냅샷 식별자 ${text(sidecar.snapshot_sha256)}`
      : '기존 동결 모델도 개인 이력 특성을 사용합니다. 이 표는 그 위에 추가한 잔차 보정만 비교합니다.';
    const prospectiveOutcomes = snapshot.prospective_outcomes || [];
    const outcomeRows = prospectiveOutcomes.map(item => [text(item.night_id), text(item.status), text(item.actual_source),
      text(item.record_id), localizedTime(item.available_at), number(item.actual_onset_offset_min, '분'),
      number(item.actual_wake_offset_min, '분'), text(item.exclusion_reason || item.rule)]);
    const outcomeTarget = byId('phone-prospective-outcome-table'); clear(outcomeTarget);
    if (outcomeRows.length) outcomeTarget.append(makeTable(['밤', '상태', '출처', '기록 ID', '확정 시각', '실제 입면', '실제 기상', '규칙/제외 사유'], outcomeRows));
    else outcomeTarget.append(el('p', '동결된 최초 자기보고 결과가 없습니다.', 'value-missing'));
    const recommendationRows = [
      ['생성 시각', localizedTime(recommendation.generated_at)], ['권장 취침', text(recommendation.bedtime)],
      ['권장 기상', text(recommendation.wake_time)], ['권장 수면시간', number(recommendation.duration_minutes, '분')],
      ['규칙성 보정', number(recommendation.regularity_minutes, '분')], ['계산 S', number(recommendation.process_s, '', 3)],
      ['실제 노출 시각', localizedTime(exposedAt)], ['노출 상태', text(exposure.status || recommendation.exposure_status, exposedAt ? '노출 확인' : '노출 미확인')],
      ['일정 실행 가능', booleanText(recommendation.plan_feasible)], ['설명', text(recommendation.explanation)]
    ];
    const recommendationTarget = byId('phone-recommendation-table'); clear(recommendationTarget); recommendationTarget.append(makeTable(['권장 항목', '저장 값'], recommendationRows));
    const replayRows = Object.entries(featureReplay.comparisons || {}).map(([key, item]) => [
      researchModelLabel(key), number(item?.expected?.onset_offset_min, '분'), number(item?.reported?.onset_offset_min, '분'), number(item?.absolute_delta_min?.onset, '분', 6),
      number(item?.expected?.wake_offset_min, '분'), number(item?.reported?.wake_offset_min, '분'), number(item?.absolute_delta_min?.wake, '분', 6), booleanText(item?.matches)
    ]);
    const replayTarget = byId('phone-replay-table'); clear(replayTarget); replayTarget.append(makeTable(['모델', '기대 입면', '보고 입면', '차이', '기대 기상', '보고 기상', '차이', '일치'], replayRows));
    byId('phone-replay-note').textContent = `허용 오차 ${number(featureReplay.tolerance_min, '분', 6)} · ${text(featureReplay.scope || featureReplay.reason)}`;
    const debugRows = scalarRows({current_snapshot_forecast_summary: snapshot.forecast, stored_feature_output_replay: featureReplay, full_pipeline_replay: replay, frozen_prospective_forecast: frozen, immutable_first_outcomes: prospectiveOutcomes});
    const debugTarget = byId('phone-output-debug'); clear(debugTarget); debugTarget.append(makeTable(['원시 항목', '저장 값'], debugRows));
    const feature = diagnostics.feature_snapshot || {};
    const physicalRows = Object.entries(feature).filter(([, value]) => value && typeof value === 'object').map(([cohort, value]) => [
      labels[cohort] || cohort, number(value.current_s, '', 3), number(value.current_c, '', 3), number(value.sleep_threshold, '', 3), number(value.wake_threshold, '', 3), booleanText(value.physical_fallback)
    ]);
    const physicalTarget = byId('phone-physical'); clear(physicalTarget); physicalTarget.append(makeTable(['코호트', 'S', 'C', '입면 임계값', '기상 임계값', 'Fallback'], physicalRows));
    fillDlFromRows(byId('phone-personalization'), scalarRows({app_recommendation_behavioral: diagnostics.app_personalization,
      frozen_research_model_behavioral: diagnostics.research_personalization,
      parameter_semantics: diagnostics.parameter_semantics,
      uncertainty: diagnostics.uncertainty,
      subjective_scale_contract: diagnostics.self_report_scales}), '개인화 계산값이 이 스냅샷에 없습니다.');
    const features = scalarRows(feature);
    const featureTarget = byId('phone-features'); clear(featureTarget); featureTarget.append(makeTable(['특성', '예측 당시 값'], features));
    const participant = currentParticipant();
    const latest = participant?.latest?.day === snapshot.day ? participant.latest : null;
    const trendRows = scalarRows({outcome_summary: snapshot.outcome_summary, observed_outcomes: diagnostics.observed_outcomes, record_trend: latest?.record_trend});
    const trends = byId('phone-trends'); clear(trends);
    const history = (participant?.days || []).slice().reverse().map(day => {
      const model = day.model_diagnostics || {};
      const appWeight = findNumeric(model.app_personalization, ['weight', 'personalization_weight', 'recent_reliability']);
      const frozenWeight = findNumeric(model.research_personalization, ['personalization_weight', 'personal_weight']);
      return [day.day, number(appWeight, '', 3), number(frozenWeight, '', 3), finite(day.record_summary?.count) ? number(day.record_summary.count, '건', 0) : '자료 없음'];
    });
    trends.append(el('h3', '날짜별 개인화 비중'));
    if (history.length > 1) trends.append(makeTable(['날짜', '추천 최근 기록 비중', '동결 모델 개인 보정 비중', '수신 기록'], history));
    else trends.append(el('p', '개인화 비중의 변화를 비교하려면 서로 다른 날짜의 스냅샷이 2개 이상 필요합니다.', 'value-missing'));
    trends.append(el('h3', '선택 날짜의 관측 결과'));
    if (trendRows.length) trends.append(makeTable(['관측 지표', '저장 값'], trendRows)); else trends.append(el('p', '이 스냅샷에는 계산 가능한 실제 결과 추세가 없습니다.', 'value-missing'));
  }
  function populateSnapshots() {
    const participant = currentParticipant(); const selector = byId('snapshot-selector'); clear(selector);
    (participant?.days || []).forEach(item => { const option = el('option', `${item.day} · 개정 ${text(item.revision)}`); option.value = `${item.day}|${item.revision}`; selector.append(option); });
    selector.disabled = !participant?.days?.length; renderSnapshot();
  }
  function populateParticipants() {
    const participants = consoleData?.phone?.participants || [];
    const selector = byId('participant-selector'); const previous = selector.value; clear(selector);
    participants.forEach(item => { const option = el('option', participantLabel(item)); option.value = `${item.device_id}|${item.participant_key}`; selector.append(option); });
    if ([...selector.options].some(option => option.value === previous)) selector.value = previous;
    selector.disabled = !participants.length;
    byId('phone-empty').hidden = Boolean(participants.length); byId('phone-analysis').hidden = !participants.length;
    populateSnapshots();
  }

  function renderFullValidity() {
    let card = byId('full-validity-report');
    if (!card) {
      card = el('article', null, 'card evidence-card'); card.id = 'full-validity-report';
      byId('target-validity-status').closest('article').after(card);
    }
    clear(card); card.append(el('h2', '확장 타당성 검증 · 1–4번 적용 결과'));
    const report = consoleData?.research?.full_validity_report;
    if (!report?.available) { card.append(el('p', '검증된 확장 연구 산출물이 없습니다.', 'value-missing')); return; }
    card.append(el('p', '기존 Test를 재사용한 탐색 분석입니다. 개인별 기간 Skill, 측정 대상, 품질 조건과 적용 가능성을 구분합니다. 추천 모델의 자동 승격이나 효과 입증이 아닙니다.', 'muted small'));
    (report.panels || []).forEach(panel => {
      const section = el('details', null, 'outcome-details');
      section.append(el('summary', panel.title));
      const table = el('div', null, 'table-scroll');
      table.append(makeTable(panel.columns, panel.rows.map(row => row.map(v => finite(v) ? number(v, '', Number.isInteger(v) ? 0 : 3) : text(v)))));
      section.append(table); panel.notes.forEach(note => section.append(el('p', note, 'muted small'))); card.append(section);
    });
  }
  function renderMeasurementAudit(snapshot) {
    let card = byId('phone-measurement-audit');
    if (!card) {
      card = el('article', null, 'card'); card.id = 'phone-measurement-audit';
      byId('phone-target-validity-status').closest('article').after(card);
    }
    clear(card); card.append(el('h2', '관측 과정·기간 변화·일정 기준선'));
    card.append(el('p', '기간 차이는 기기 오류나 치료 효과의 증거가 아닙니다. 백업에서 처음 확인한 시각은 최초 입력시각과 다르며, 없는 착용·OS 정보는 추정하지 않습니다.', 'muted small'));
    const audit = snapshot.measurement_audit;
    if (!audit) { card.append(el('p', '관측 과정 분석 자료 없음', 'value-missing')); return; }
    const titles = {record_validation:'입력·시간 검증',enrollment_coverage:'등록기간의 관측 범위',duration_semantics:'수면 분량과 시간구간의 정의',
      episode_selection_sensitivity:'주수면 선택 규칙 민감도',fixed_14_day_periods:'첫·최근 14일의 출처별 차이',first_snapshot_appearance_lag:'수신 백업에서 처음 확인된 지연',
      direct_context:'직접 입력된 생활 맥락',unavailable_dimensions:'아직 측정되지 않은 항목',first_outcome_reporting_lag:'최초 자기보고 결과의 등록 지연'};
    Object.entries(titles).forEach(([key,title]) => {
      const section=el('details',null,'outcome-details');section.append(el('summary',title));
      const table=el('div',null,'table-scroll');table.append(makeTable(['항목','관측값'],scalarRows(audit[key] || {})));section.append(table);card.append(section);
    });
    const baseline = snapshot.research_baseline_evaluation;
    if (baseline) {
      card.append(el('h3','미리 저장한 단순 기준선의 예측 오차'));
      const comparisonTable=el('div',null,'table-scroll');
      comparisonTable.append(makeTable(['기준선·모델','유효 밤','입면 MAE(분)','기상 MAE(분)','공동 MAE(분)','같은 밤 수','14일 평균 대비 ΔMAE(분)','Skill'],(baseline.models || []).map(m=>[m.name,m.n_nights,number(m.onset_mae_min),number(m.wake_mae_min),number(m.joint_mae_min),m.paired_reference_nights,number(m.paired_delta_mae_min),number(m.paired_skill_vs_mean14)])));
      card.append(comparisonTable);
      card.append(el('p',baseline.interpretation,'muted small'));
    }
  }
  async function load() {
    setError('research-console-error', ''); setError('phone-console-error', '');
    try {
      const response = await fetch('/api/research-console', {headers: {'Accept': 'application/json'}});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '연구 진단 자료를 불러오지 못했습니다.');
      consoleData = result;
      byId('research-console-status').textContent = statusText(result.research?.status);
      byId('phone-console-status').textContent = `${statusText(result.phone?.status)} · ${result.phone?.participant_count ?? 0}명`;
      populateCohorts(); renderPersonalizationValidation(); renderContinuousConstantsValidation(); renderTargetValidityValidation(); renderFullValidity(); populateParticipants();
    } catch (error) {
      byId('research-console-status').textContent = '연결 실패'; byId('phone-console-status').textContent = '연결 실패';
      setError('research-console-error', error.message); setError('phone-console-error', error.message);
      byId('phone-empty').hidden = false; byId('phone-analysis').hidden = true;
    } finally {
      byId('research-console-refresh').disabled = false;
    }
  }

  byId('cohort-selector').addEventListener('change', renderCohort);
  byId('participant-selector').addEventListener('change', populateSnapshots);
  byId('snapshot-selector').addEventListener('change', renderSnapshot);
  byId('research-console-refresh').addEventListener('click', () => {
    byId('research-console-refresh').disabled = true;
    byId('phone-console-status').textContent = '새 자료 확인 중';
    load();
  });
  document.addEventListener('sleepwell:view', event => {
    const view = event.detail?.view;
    if (view === 'research') { byId('greeting').textContent = '모델 연결과 성능을 검토합니다.'; byId('subtitle').textContent = '동일 코호트에서 물리·데이터·융합 모델과 개인화 계산을 확인합니다.'; }
    if (view === 'phone-research') { byId('greeting').textContent = '휴대폰 참여자 자료를 분석합니다.'; byId('subtitle').textContent = '수신한 스냅샷의 모델 출력, 입력 특성, 개인화 상태와 관측 추세를 확인합니다.'; }
  });
  load();
})();
