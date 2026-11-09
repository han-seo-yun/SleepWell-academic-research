// Unified shell views: overview, participants, embedded research lab, integrations.
// Reads only the read-only /api/v1 surface; app.js keeps owning navigation (setView).
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const fmt = (value, digits = 1, unit = '') => (value == null || !Number.isFinite(Number(value)) ? '—' : `${Number(value).toFixed(digits)}${unit}`);
  const span = minutes => {
    if (minutes == null || !Number.isFinite(Number(minutes))) return '—';
    const value = Number(minutes);
    if (Math.abs(value) >= 1440) return `${(value / 1440).toFixed(1)}일`;
    if (Math.abs(value) >= 120) return `${(value / 60).toFixed(1)}시간`;
    return `${Math.round(value)}분`;
  };
  const pct = value => (value == null ? '—' : `${Math.round(Number(value) * 100)}%`);
  const when = value => {
    if (!value) return '—';
    try {
      return new Intl.DateTimeFormat('ko-KR', {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul'}).format(new Date(value));
    } catch { return '—'; }
  };
  const statusLabel = {timely: '다음 20시 전 입력', late: '늦은 입력', prediction_only: '예측만', missing: '기록 없음'};
  let system = null;
  let labSource = null;
  let selectedParticipant = null;

  async function getJSON(path) {
    const response = await fetch(path, {headers: {Accept: 'application/json'}});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '자료를 불러오지 못했습니다.');
    return body;
  }

  function show(view) {
    const button = document.querySelector(`.nav[data-view="${view}"]`);
    if (button) button.click();
  }

  // ---------- theme ----------
  function syncThemeButtons() {
    const current = window.sleepwellTheme ? window.sleepwellTheme.get() : 'system';
    document.querySelectorAll('[data-theme-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === current)));
  }
  document.querySelectorAll('[data-theme-choice]').forEach(button => button.addEventListener('click', () => {
    if (window.sleepwellTheme) window.sleepwellTheme.set(button.dataset.themeChoice);
    syncThemeButtons();
  }));
  syncThemeButtons();

  // ---------- system ----------
  async function loadSystem() {
    try {
      system = await getJSON('/api/v1/system');
    } catch {
      system = {};
    }
    if (system.app_version) $('app-version').textContent = `v${system.app_version}`;
    if (system.platform) $('platform-badge').textContent = `연구자용 ${system.platform} 소프트웨어`;
    const lab = Boolean(system.research_lab_url);
    $('nav-lab').hidden = !lab;
    document.querySelectorAll('[data-requires-lab]').forEach(item => { item.hidden = !lab; });
    $('api-version-badge').textContent = `${system.api_version || 'v1'} · app ${system.app_version || 'dev'}`;
    renderPhoneStatus();
  }

  function renderPhoneStatus() {
    const target = $('integration-phone-status');
    const mobile = system?.mobile;
    if (!mobile) { target.textContent = '이 실행 환경에는 휴대폰 수신 기능이 없습니다(데스크톱 앱에서 제공).'; return; }
    const devices = mobile.devices || [];
    target.textContent = `${mobile.enabled ? '수신 중' : '수신 꺼짐'} · 등록 기기 ${devices.length}대${devices[0]?.last_sync ? ` · 최근 동기화 ${when(devices[0].last_sync)}` : ''}`;
  }

  // ---------- overview ----------
  function kpi(label, value, note, tone = '') {
    return `<div class="kpi ${tone}"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  }

  function strip(cells) {
    return `<span class="strip" role="img" aria-label="최근 ${cells.length}일 기록 상태">${cells.map(cell => `<i class="${esc(cell.status)}" title="${esc(cell.date)} · ${esc(statusLabel[cell.status] || cell.status)}"></i>`).join('')}</span>`;
  }

  async function loadOverview() {
    const kpis = $('overview-kpis');
    try {
      const [data, list] = await Promise.all([getJSON('/api/v1/overview'), getJSON('/api/v1/participants')]);
      const p = data.prospective || {};
      const tonight = p.tonight_captured ? (p.tonight_eligible ? '저장됨' : '참고용 저장') : (data.profile_ready ? '20:00 대기' : '프로필 없음');
      kpis.innerHTML = [
        kpi('참여자', `${data.participants.total}명`, `휴대폰 ${data.participants.phone}명 · 기록 있음 ${data.participants.with_records}명`),
        kpi('관측된 밤', `${data.participants.nights}밤`, '참여자 전체 주 수면 기록'),
        kpi('오늘 사전 예측', tonight, `주 평가 누적 ${p.eligible_captures ?? 0}밤 · 실제 결과 연결 ${p.evaluated_nights ?? 0}밤`, p.tonight_captured ? 'is-ok' : ''),
        kpi('확인 필요', `${data.alerts.length}건`, data.alerts.length ? '아래 목록을 확인하세요' : '경고 없음', data.alerts.length ? 'is-warn' : 'is-ok'),
      ].join('');
      const count = $('nav-alert-count');
      count.hidden = data.alerts.length === 0;
      count.textContent = String(data.alerts.length);
      $('overview-alerts').innerHTML = data.alerts.length
        ? data.alerts.map(alert => `<li><span><strong>${esc(alert.participant)}</strong> · ${esc(alert.text)}</span><button class="text-button" type="button" data-open-participant="${esc(alert.participant_id)}">보기 →</button></li>`).join('')
        : '<li class="is-empty">현재 확인이 필요한 항목이 없습니다.</li>';
      const statusRow = (label, state, text) => `<dt>${esc(label)}</dt><dd><span class="dot ${state}"></span>${esc(text)}</dd>`;
      const mobile = data.mobile;
      $('overview-status').innerHTML = [
        statusRow('연구 모델', data.models.available ? 'ok' : 'warn', data.models.available ? `CMI·MESA ${data.models.runtime_model_version || ''}` : (data.models.reason || '사용 불가')),
        statusRow('연구 분석실', data.research_lab_url ? 'ok' : 'off', data.research_lab_url ? '실행 중 · 같은 기록 읽기 전용' : '데스크톱 앱에서 제공'),
        statusRow('휴대폰 수신', mobile ? (mobile.enabled ? 'ok' : 'off') : 'off', mobile ? `${mobile.enabled ? '수신 중' : '꺼짐'} · 등록 ${mobile.devices?.length || 0}대` : '이 환경에서 사용 불가'),
        statusRow('20시 자동 저장', p.capture_error ? 'warn' : 'ok', p.capture_error || `다음 결정 ${when(p.next_decision_at)}`),
        statusRow('앱 버전', 'ok', `${data.app_version} · API ${data.api_version}`),
      ].join('');
      const rows = list.participants.map(item => `<tr data-open-participant="${esc(item.id)}"><td>${esc(item.label)}</td><td>${strip(item.recent_strip || [])}</td><td>${item.recent_observed ?? 0}/${item.recent_days ?? 14}</td><td>${esc(when(item.last_wake))}</td></tr>`).join('');
      $('overview-strips').innerHTML = rows
        ? `<table class="participant-table"><thead><tr><th>참여자</th><th>최근 14일</th><th>관측</th><th>마지막 기상</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<p class="empty-state">아직 참여자 기록이 없습니다.</p>';
    } catch (error) {
      kpis.innerHTML = `<p class="console-alert">${esc(error.message)}</p>`;
    }
  }

  // ---------- participants ----------
  async function loadParticipants() {
    const target = $('participant-list');
    try {
      const data = await getJSON('/api/v1/participants');
      const t = data.thresholds || {};
      $('participant-thresholds').textContent = `경고 기준: 마지막 기록 ${t.stale_record_days}일 이상 · 최근 ${t.recent_days}일 관측 ${pct(t.low_coverage)} 미만 · 늦은 입력 ${pct(t.high_late_share)} 초과 · 휴대폰 동기화 ${t.stale_sync_days}일 이상. ${data.note}`;
      if (!data.participants.length) { target.innerHTML = '<p class="empty-state">아직 참여자가 없습니다. 시뮬레이션 프로필을 만들거나 휴대폰을 연결하세요.</p>'; return; }
      target.innerHTML = `<table class="participant-table"><thead><tr><th>참여자</th><th>유형</th><th>관측 밤</th><th>최근 14일</th><th>마지막 기상</th><th>마지막 동기화</th><th>SRI</th><th>평균 중앙시각</th><th>상태</th></tr></thead><tbody>${data.participants.map(item => `<tr data-open-participant="${esc(item.id)}" aria-selected="${item.id === selectedParticipant}" tabindex="0"><td>${esc(item.label)}</td><td>${item.kind === 'pc' ? 'PC' : '휴대폰'}</td><td>${item.nights ?? 0}</td><td>${strip(item.recent_strip || [])} ${item.recent_observed ?? 0}/${item.recent_days ?? 14}</td><td>${esc(when(item.last_wake))}</td><td>${item.kind === 'phone' ? esc(when(item.last_sync)) : '—'}</td><td>${fmt(item.sri)}</td><td>${esc(item.midpoint_mean_clock || '—')}${item.midpoint_sd_min != null ? ` ±${fmt(item.midpoint_sd_min, 0)}분` : ''}</td><td>${(item.alerts || []).filter(a => a.level !== 'info').length ? `<span class="badge warn">확인 ${(item.alerts || []).filter(a => a.level !== 'info').length}</span>` : '<span class="badge ok">정상</span>'}</td></tr>`).join('')}</tbody></table><div class="legend-row"><span><i class="timely"></i>다음 20시 전 입력</span><span><i class="late"></i>늦은 입력</span><span><i class="prediction_only"></i>예측만</span><span><i class="missing"></i>기록 없음</span></div>`;
      if (selectedParticipant) loadDetail(selectedParticipant);
    } catch (error) {
      target.innerHTML = `<p class="console-alert">${esc(error.message)}</p>`;
    }
  }

  function actogram(nights) {
    // Single-plotted 24 h from the 20:00 cutoff, newest night at the bottom.
    const rows = nights.slice(-60);
    if (!rows.some(night => night.onset_offset_min != null)) return '<p class="empty-state">표시할 관측 수면 구간이 없습니다.</p>';
    const left = 48, width = 720, rowH = 9, gap = 3, top = 6;
    const height = top + rows.length * (rowH + gap) + 24;
    const x = minutes => left + (minutes / 1440) * width;
    const parts = [];
    for (let hour = 0; hour <= 24; hour += 4) {
      const xx = x(hour * 60);
      parts.push(`<line class="gridline" x1="${xx}" y1="${top}" x2="${xx}" y2="${height - 20}"/>`);
      parts.push(`<text x="${xx}" y="${height - 6}" text-anchor="middle">${String((20 + hour) % 24).padStart(2, '0')}:00</text>`);
    }
    rows.forEach((night, index) => {
      const y = top + index * (rowH + gap);
      parts.push(`<rect class="row-bg" x="${left}" y="${y}" width="${width}" height="${rowH}" rx="2"/>`);
      if (night.onset_offset_min == null) {
        parts.push(`<rect class="unknown" x="${left}" y="${y}" width="${width}" height="${rowH}" rx="2"><title>${esc(night.cutoff.slice(0, 10))} · 결과 없음</title></rect>`);
      } else {
        const start = x(night.onset_offset_min), end = x(Math.min(1440, night.wake_offset_min));
        parts.push(`<rect class="sleep" x="${start.toFixed(1)}" y="${y}" width="${Math.max(1, end - start).toFixed(1)}" height="${rowH}" rx="2"><title>${esc(night.cutoff.slice(0, 10))} · ${esc(night.sleep_start?.slice(11, 16))}~${esc(night.sleep_end?.slice(11, 16))}${night.timely === false ? ' · 다음 20시 이후 입력' : ''}</title></rect>`);
        if (night.timely === false) parts.push(`<rect class="late-mark" x="${left - 8}" y="${y}" width="4" height="${rowH}" rx="1"/>`);
      }
      if (index % 7 === 0 || index === rows.length - 1) parts.push(`<text x="${left - 6}" y="${y + rowH - 1}" text-anchor="end">${esc(night.cutoff.slice(5, 10))}</text>`);
    });
    return `<svg class="actogram" viewBox="0 0 ${left + width + 24} ${height}" role="img" aria-label="최근 ${rows.length}개 기준일의 관측 주 수면 구간 액토그램">${parts.join('')}</svg>`;
  }

  async function loadDetail(id) {
    const target = $('participant-detail');
    selectedParticipant = id;
    document.querySelectorAll('#participant-list tr[data-open-participant]').forEach(row => row.setAttribute('aria-selected', String(row.dataset.openParticipant === id)));
    target.hidden = false;
    target.innerHTML = '<p class="muted">불러오는 중…</p>';
    try {
      const data = await getJSON(`/api/v1/participants/${encodeURIComponent(id)}`);
      const p = data.participant, r = data.regularity || {}, c = data.completeness || {};
      const metric = (label, value) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
      const coverage = p.context_coverage || {};
      target.innerHTML = `<div class="detail-head"><div><span class="eyebrow">${p.kind === 'pc' ? 'PC PARTICIPANT' : 'PHONE PARTICIPANT'}</span><h2>${esc(p.label)}</h2><p class="muted small">${esc(c.first_date || '—')} ~ ${esc(c.last_date || '—')} · 관측 ${p.nights}밤${p.device_name ? ` · 기기 ${esc(p.device_name)}` : ''}</p></div>
        <div class="detail-actions"><a class="secondary compact" href="${esc(data.links.records_csv)}" download>표준 CSV 받기</a><button class="primary compact" type="button" data-analyze-participant="${esc(p.id)}" ${system?.research_lab_url ? '' : 'hidden'}>연구 분석실에서 분석 →</button></div></div>
        ${(p.alerts || []).length ? `<ul class="alert-list">${p.alerts.map(alert => `<li>${esc(alert.text)}</li>`).join('')}</ul>` : ''}
        <div class="metric-row">${metric('SRI (−100~100)', fmt(r.sri))}${metric('IS / IV', `${fmt(r.interdaily_stability, 2)} / ${fmt(r.intradaily_variability, 2)}`)}${metric('평균 입면·기상', `${r.onset_mean_clock || '—'} · ${r.wake_mean_clock || '—'}`)}${metric('중앙시각 SD', fmt(r.midpoint_sd_min, 0, '분'))}${metric('20시 전 입력', pct(c.timely_share_of_labeled))}${metric('입력 지연 중앙값', span(c.report_delay_median_min))}</div>
        <h3>관측 수면 액토그램 (최근 ${Math.min(60, data.nights.length)}개 기준일)</h3>${actogram(data.nights)}
        <div class="legend-row"><span><i class="observed"></i>관측 주 수면</span><span><i class="missing"></i>결과 없음</span><span><i class="late"></i>다음 20시 이후 입력(왼쪽 표시)</span></div>
        <h3>생활 정보 수신</h3><p class="muted small">수면 ${coverage.sleep ?? 0} · 걸음 ${coverage.steps ?? 0} · 운동 ${coverage.exercise ?? 0} · 카페인 ${coverage.caffeine ?? 0} · 일정 ${coverage.schedule ?? 0} · 낮잠 ${coverage.nap ?? 0}건</p>
        ${r.unavailable?.length ? `<p class="muted small">계산 불가: ${esc(r.unavailable.join(' / '))}</p>` : ''}
        <details class="outcome-details"><summary>계산 가정과 자료 경고</summary><ul class="note-list">${Object.values(data.assumptions || {}).concat(data.warnings || []).map(text => `<li>${esc(text)}</li>`).join('')}</ul></details>`;
    } catch (error) {
      target.innerHTML = `<p class="console-alert">${esc(error.message)}</p>`;
    }
  }

  // ---------- lab ----------
  function openLab() {
    const frame = $('lab-frame');
    if (!system?.research_lab_url) return;
    const url = new URL(system.research_lab_url);
    url.hash = `embed=1&theme=${encodeURIComponent(window.sleepwellTheme ? window.sleepwellTheme.get() : 'system')}`;
    if (labSource) url.hash += `&source=${encodeURIComponent(labSource)}`;
    if (frame.dataset.loaded !== url.toString()) {
      frame.src = url.toString();
      frame.dataset.loaded = url.toString();
    }
    labSource = null;
  }

  // ---------- integrations ----------
  function renderIntegrations() {
    const endpoints = [
      ['/api/v1', '색인·버전'], ['/api/v1/system', '앱·모델 버전, 분석실 주소, 휴대폰 수신 상태'],
      ['/api/v1/overview', '대시보드 요약'], ['/api/v1/participants', '참여자 목록·최근 14일·경고'],
      ['/api/v1/participants/{id}', '규칙성·완결성·최근 120밤'], ['/api/v1/participants/{id}/records.csv', '표준 5열 CSV (분석실·CLI 입력)'],
      ['/api/v1/openapi.json', 'OpenAPI 3.1 문서'], ['/api/export', 'PC 참가자 전체 기록 JSON'],
    ];
    $('api-table').innerHTML = `<table class="api-table"><thead><tr><th>GET 경로</th><th>내용</th></tr></thead><tbody>${endpoints.map(([path, text]) => `<tr><td><code>${esc(path)}</code></td><td>${esc(text)}</td></tr>`).join('')}</tbody></table>`;
    const origin = window.location.origin;
    $('api-example').textContent = `# 이 컴퓨터에서만 응답합니다 (실행할 때마다 포트가 바뀔 수 있음)\ncurl ${origin}/api/v1/participants\ncurl -o pc.csv ${origin}/api/v1/participants/pc/records.csv\npython research_lab_cli.py --csv pc.csv --out 결과/pc-run1`;
    getJSON('/api/v1/participants').then(data => {
      const links = data.participants.filter(item => item.nights).map(item => `<a class="secondary compact" href="/api/v1/participants/${encodeURIComponent(item.id)}/records.csv" download>${esc(item.label)} CSV</a>`).join('');
      $('integration-exports').innerHTML = `<a class="secondary compact" href="/api/export" download>PC 전체 기록 JSON</a>${links}`;
    }).catch(() => {});
  }

  // ---------- wiring ----------
  document.addEventListener('click', event => {
    const open = event.target.closest('[data-open-participant]');
    if (open) {
      selectedParticipant = open.dataset.openParticipant;
      if (!$('participants').hidden) loadDetail(selectedParticipant);
      else show('participants');
      return;
    }
    const analyze = event.target.closest('[data-analyze-participant]');
    if (analyze) { labSource = analyze.dataset.analyzeParticipant; show('lab'); return; }
    const scroll = event.target.closest('[data-scroll-to]');
    if (scroll) $(scroll.dataset.scrollTo)?.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  document.addEventListener('keydown', event => {
    const row = event.target.closest?.('tr[data-open-participant]');
    if (row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); row.click(); }
  });
  document.addEventListener('sleepwell:theme', () => {
    // Follow the shell theme inside the lab without reloading it (no re-import either).
    const frame = $('lab-frame');
    if (!frame.dataset.loaded) return;
    const url = new URL(frame.dataset.loaded);
    url.hash = `embed=1&theme=${encodeURIComponent(window.sleepwellTheme.get())}`;
    frame.dataset.loaded = url.toString();
    frame.src = url.toString();
  });
  $('overview-refresh').addEventListener('click', loadOverview);
  $('participants-refresh').addEventListener('click', loadParticipants);
  const SHELL_VIEWS = new Set(['overview', 'participants', 'lab', 'integrations', 'research', 'phone-research']);
  document.addEventListener('sleepwell:view', event => {
    const view = event.detail.view;
    document.body.classList.toggle('shell-view', SHELL_VIEWS.has(view));
    if (view === 'overview') loadOverview();
    if (view === 'participants') loadParticipants();
    if (view === 'lab') openLab();
    if (view === 'integrations') { renderIntegrations(); loadSystem(); }
  });

  // The phone connection card lives under 기기 연결·API in the unified shell.
  const external = $('desktop-research-lab');
  if (external) { external.target = '_blank'; external.rel = 'noopener'; }
  const card = $('mobile-card');
  if (card) $('mobile-card-slot').appendChild(card);
  loadSystem().then(loadOverview);
})();
