let state;
let currentView = 'overview';
const viewedRecommendations = new Set();
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clock = value => new Intl.DateTimeFormat('ko-KR',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Seoul'}).format(new Date(value));
const day = value => new Intl.DateTimeFormat('ko-KR',{month:'long',day:'numeric',weekday:'short',timeZone:'Asia/Seoul'}).format(new Date(value));
const duration = minutes => minutes == null ? '아직 기록 없음' : `${Math.floor(minutes / 60)}시간 ${Math.round(minutes % 60)}분`,
  sleepEstimate = minutes => minutes == null ? '추정 불가/입면·각성 미입력' : duration(minutes),
  evidenceQualityText = parameters => {
    const quality=parameters?.evidence_quality;
    if(!quality)return '근거 품질 정보 없음';
    const labels={insufficient_effective_records:'기록 희소',stale_history:'기록 오래됨',irregular_midpoint:'수면 중간시각 불규칙'};
    const active=(quality.flags||[]).filter(flag=>flag.active).map(flag=>labels[flag.code]||flag.code);
    return `운영 휴리스틱 (보정된 확률·임상적 확실성 아님): ${active.length?active.join(' · '):'활성 플래그 없음'}`;
  };
const forecastLabels={
  profile_baseline:'가입 정보 기준',recent_adaptive:'최근 기록 반영',context_candidate:'일정·생활 맥락 후보',
  physical:'수리 모델 (기존)',data:'데이터 모델 (기존)',hybrid:'융합 모델 (기존)',hybrid_frozen:'개인 보정 없는 융합',
  physical_cmi:'수리 모델 · CMI',data_cmi:'데이터 모델 · CMI',hybrid_cmi:'융합 모델 · CMI',
  physical_mesa:'수리 모델 · MESA',data_mesa:'데이터 모델 · MESA',hybrid_mesa:'융합 모델 · MESA'
};
let contextLoadedNight=null;
let contextDirty=false;
const safeClock=value=>{try{return value?clock(value):'—';}catch{return '—';}};
const metric=value=>value!=null&&value!==''&&Number.isFinite(Number(value))?`${Number(value).toFixed(1)}분`:'—';
const goalDifferenceText=recommendation=>recommendation?.sleep_debt_status==='available'&&recommendation.sleep_debt_min!=null&&Number.isFinite(Number(recommendation.sleep_debt_min))?`${duration(Number(recommendation.sleep_debt_min))}`:'계산 불가/완전 일지 없음';
const nonBiologicalDifferenceWording=value=>{
  const text=String(value??'');
  if(!text.includes('수면부족분'))return text;
  return `${text.replaceAll('관측 수면부족분','희망 목표 대비 차이').replaceAll('수면부족분','희망 목표 대비 차이')} 생리적 수면부족이나 필요량을 뜻하지 않습니다.`;
};
function inputDateTime(value){
  if(!value)return '';
  if(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value))return value.slice(0,16);
  const date=new Date(value);if(Number.isNaN(date.getTime()))return '';
  const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(date);
  const part=type=>parts.find(x=>x.type===type)?.value;
  return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`;
}
function setDefaultRecordTimes(){
  const form=$('record-form'),today=state?.today;if(!form||!today)return;
  const yesterday=new Date(`${today}T12:00:00+09:00`);yesterday.setDate(yesterday.getDate()-1);
  const prior=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul'}).format(yesterday);
  form.elements.bed.value=`${prior}T23:30`;form.elements.wake.value=`${today}T07:00`;
}
function clearRecordEdit(reset=true){
  const form=$('record-form');if(reset)form.reset();
  form.elements.replaces_date.value='';$('record-edit-state').hidden=true;$('record-edit-label').textContent='';
  if(reset)setDefaultRecordTimes();
}
function startRecordEdit(recordDate){
  const record=state.records.find(item=>item.date===recordDate&&item.source!=='archive');if(!record)return;
  const form=$('record-form');
  for(const key of ['bed','wake'])form.elements[key].value=inputDateTime(record[key]);
  for(const key of ['latency_min','awake_min','rested','fatigue','sleepiness','burden','survey_occurred_at','steps','exercise_min','note'])form.elements[key].value=key==='survey_occurred_at'?inputDateTime(record[key]):record[key]??'';
  form.elements.replaces_date.value=record.date;
  $('record-edit-label').textContent=`${record.date} 기상 기록을 수정 중입니다.`;$('record-edit-state').hidden=false;
  setView('record');form.scrollIntoView({behavior:'smooth',block:'start'});
}
function repeatRow(type,value={}){
  const row=document.createElement('div');row.className='repeat-row';row.dataset.type=type;
  if(type==='schedule')row.innerHTML=`<label>종류<select data-field="kind"><option value="work">근무</option><option value="class">수업</option><option value="first">첫 일정</option></select></label><label>시작<input data-field="start" type="datetime-local" required></label><label>종료 (선택)<input data-field="end" type="datetime-local"></label><button class="remove-row" type="button" aria-label="일정 삭제">삭제</button>`;
  if(type==='nap')row.innerHTML=`<label>시작<input data-field="start" type="datetime-local" required></label><label>종료<input data-field="end" type="datetime-local" required></label><button class="remove-row" type="button" aria-label="낮잠 삭제">삭제</button>`;
  if(type==='caffeine')row.innerHTML=`<label>섭취 시각<input data-field="at" type="datetime-local" required></label><label>카페인 (mg)<input data-field="mg" type="number" min="0" max="1000" step="1" required placeholder="예: 80"></label><button class="remove-row" type="button" aria-label="카페인 기록 삭제">삭제</button>`;
  Object.entries(value).forEach(([key,item])=>{const field=row.querySelector(`[data-field="${key}"]`);if(field)field.value=['start','end','at'].includes(key)?inputDateTime(item):item;});
  row.querySelector('.remove-row').addEventListener('click',()=>{row.remove();contextDirty=true;updateAddButtons();});
  row.querySelectorAll('input,select').forEach(input=>input.addEventListener('input',()=>{contextDirty=true;}));
  return row;
}
function addRepeatRow(type,value={}){
  const container=$(`${type}-rows`);if(!container||container.children.length>=12)return;
  container.appendChild(repeatRow(type,value));contextDirty=true;updateAddButtons();
}
function updateAddButtons(){
  for(const type of ['schedule','nap','caffeine']){const button=$(`add-${type}`),container=$(`${type}-rows`);if(button&&container)button.disabled=container.children.length>=12;}
}
function populateContext(context,nightId){
  const form=$('context-form');if(!form)return;
  form.elements.night_id.value=nightId||state?.today||'';
  form.elements.preparation_min.value=context?.preparation_min??30;
  for(const type of ['schedule','nap','caffeine'])$(`${type}-rows`).replaceChildren();
  (context?.schedules||[]).forEach(value=>$('schedule-rows').appendChild(repeatRow('schedule',value)));
  (context?.naps||[]).forEach(value=>$('nap-rows').appendChild(repeatRow('nap',value)));
  (context?.caffeine||[]).forEach(value=>$('caffeine-rows').appendChild(repeatRow('caffeine',value)));
  form.elements.context_confirmed.checked=Boolean(context);
  contextLoadedNight=nightId;contextDirty=false;updateAddButtons();
  $('context-save-state').textContent=context?`저장됨${context.observed_at?` · ${safeClock(context.observed_at)}`:''}`:'미입력 · 아직 저장하지 않음';
}
function readRows(type){
  return [...$(`${type}-rows`).children].map(row=>Object.fromEntries([...row.querySelectorAll('[data-field]')].map(field=>[field.dataset.field,field.dataset.field==='mg'?Number(field.value):field.value||null])));
}
function renderProspective(prospective,targetNight){
  const latest=prospective?.current;
  const current=latest?.night_id===targetNight?latest:null;
  const summary=prospective?.summary;
  const primary=prospective?.primary_diary_summary||summary;
  const auxiliary=prospective?.auxiliary_wearable_summary;
  const captureLabels={primary_window:'20:00~20:15 최초 저장',late_generation:'20:15 이후 참고용 저장',invalid_capture_timestamp:'저장 시각 오류',invalid_cutoff:'기준 시각 오류'};
  $('prospective-status').textContent=current?(current.eligible_capture?'주 평가에 포함':'참고용 저장'):'오늘 예측 대기';
  if(current){
    const rows=Object.entries(current.predictions||{}).map(([arm,p])=>`<tr><td>${escapeHTML(forecastLabels[arm]||arm)}</td><td>${safeClock(p.bed)}</td><td>${safeClock(p.wake)}</td><td>${duration(p.interval_min)}</td></tr>`).join('');
    const overlaps=current.feature_snapshot?.context?.commitment_overlap||[];
    const overlapText=overlaps.length?`<div class="context-overlap"><strong>일정 겹침 후보 ${overlaps.length}건</strong><ul>${overlaps.map(item=>`<li>${escapeHTML({work:'근무',class:'수업',first:'첫 일정'}[item.kind]||item.kind||'일정')} · ${safeClock(item.start)}~${safeClock(item.end)}</li>`).join('')}</ul><span>예상 수면 구간과 입력 일정이 겹친다는 경고이며, 실제로 가능한 일정인지 판정한 결과는 아닙니다.</span></div>`:'';
    $('prospective-current').innerHTML=`<p><strong>${escapeHTML(current.night_id)}의 고정 예측</strong> · ${safeClock(current.generated_at)} 저장 · 모델 ${escapeHTML(current.model_version||'버전 미표시')}</p><div class="table-scroll"><table><thead><tr><th>비교 모델</th><th>예상 수면 시작</th><th>예상 기상</th><th>시작~기상 간격 (불확실성 구간 아님)</th></tr></thead><tbody>${rows||'<tr><td colspan="4">저장된 예측값이 없습니다.</td></tr>'}</tbody></table></div>${overlapText}<p class="muted small">${escapeHTML(captureLabels[current.capture_reason]||current.capture_reason||'최초 저장 시각 기준')} · 기준 시각 ${safeClock(current.cutoff)}</p>`;
  }else{
    const next=prospective?.next_decision_at;
    const nextText=next?`${day(next)} ${safeClock(next)}`:'오늘 20:00';
    $('prospective-current').innerHTML=`<p class="empty-state">${escapeHTML(targetNight||'오늘')}의 사전 예측은 아직 사용할 수 없습니다. ${escapeHTML(nextText)}에 당시까지 저장된 정보로 첫 예측을 고정합니다.</p>`;
  }
  const summaryTable=(data,title)=>{
    const totals=`저장 ${data.total_forecasts??0}밤 · 주 평가 ${data.eligible_captures??0}밤 · 실제 결과 연결 ${data.evaluated_nights??0}밤 · 대기 ${data.pending_nights??0}밤 · 제외 ${data.excluded_nights??0}밤`;
    const rows=(data.versions||[]).flatMap(version=>Object.entries(version.source_strata||{unclassified:{arms:version.arms||{}}}).flatMap(([source,group])=>Object.entries(group.arms||{}).map(([arm,m])=>`<tr><td>${escapeHTML(version.model_version||'버전 미표시')}<br>${escapeHTML({user_reported_primary:'직접 일지 · 주 지표',automatic_wearable_auxiliary:'웨어러블 · 보조 지표',unclassified:'기존 통합 집계'}[source]||source)}</td><td>${escapeHTML(forecastLabels[arm]||arm)}</td><td>${m.n??0}</td><td>${`${metric(m.onset_mae_min)} (n=${m.onset_n ?? m.n ?? 0})`}</td><td>${`${metric(m.wake_mae_min)} (n=${m.wake_n ?? m.n ?? 0})`}</td><td>${`${metric(m.joint_mae_min)} (n=${m.joint_n ?? m.n ?? 0})`}</td><td>${`${metric(m.interval_mae_min)} (n=${m.interval_n ?? m.n ?? 0})`}</td></tr>`))).join('');
    return `<h3>${escapeHTML(title)}</h3><p>${totals}</p>${rows?`<div class="table-scroll"><table><thead><tr><th>모델 버전</th><th>비교 모델</th><th>평가 밤</th><th>시작 MAE</th><th>기상 MAE</th><th>평균 MAE</th><th>간격 MAE</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p class="muted small">실제 수면이 연결되면 모델별 오차가 표시됩니다.</p>'}<p class="muted small">${escapeHTML(data.explanation||data.note||'')}</p>`;
  };
  $('prospective-summary').innerHTML=primary?summaryTable(primary,'직접 일지 기준 주 평가')+(auxiliary?summaryTable(auxiliary,'웨어러블 기준 보조 평가'):''):'<p class="muted small">아직 집계할 고정 예측이 없습니다.</p>';
  if(prospective?.capture_error)$('prospective-current').textContent=prospective.capture_error;
  const corrections=Number(summary?.corrections_count||0);
  $('prospective-corrected').innerHTML=corrections>0&&prospective?.corrected_summary?`${summaryTable(prospective.corrected_summary,'수정된 최신 실제 결과 참고 집계')}<p class="muted small">실제 결과 수정 ${corrections}건을 반영한 참고 집계입니다. 주 평가는 최초 입력 결과를 그대로 보존합니다.</p>`:'';
  const withdrawn=prospective?.retracted_health_connect_assessments||0;
  const warnings=[current?.warning,prospective?.capture_error,prospective?.note,withdrawn?`휴대폰에서 삭제되거나 다른 주 수면으로 대체되어 현재 평가에서 제외된 결과 ${withdrawn}건. 과거 평가 이력은 보존합니다.`:null].filter(Boolean);
  $('prospective-warning').innerHTML = (()=>{
      const evidenceTarget=$('forecast-evidence-status');
      const intervals=current?.prediction_intervals;
      const monitoring=prospective?.interval_monitoring;
      const statusLabels={not_registered:'구간 모니터링 미등록',awaiting_future_data:'미래 자료 대기',calibration_in_progress:'미래 보정 진행 중',awaiting_interval_calibration:'구간 보정 대기',insufficient_calibration:'보정 표본 부족',calibrated_heldout_pending:'보정 전용 구간 · 별도 미래 보류 결과 대기',heldout_period_complete:'미래 보류 기간 종료 · 기술통계만'};
      if(!current||!intervals){
        evidenceTarget.innerHTML='<strong>예측구간 검증 정보 없음</strong><br>보정·미래 보류 근거가 없어 수치 예측구간을 숨깁니다.';
      }else{
        const nominal=intervals.nominal_joint_coverage==null?null:Number(intervals.nominal_joint_coverage);
        const intervalRows=Object.entries(intervals.arms||{}).map(([arm,value])=>`<tr><td>${escapeHTML(forecastLabels[arm]||arm)}</td><td>${safeClock(value?.onset?.lower)}~${safeClock(value?.onset?.upper)}</td><td>${safeClock(value?.wake?.lower)}~${safeClock(value?.wake?.upper)}</td><td>${value?.joint_radius_min==null?'—':`${Number(value.joint_radius_min).toFixed(1)}분`}</td></tr>`).join('');
        const groups=(monitoring?.groups||[]).filter(group=>!current.monitoring_lock_sha256||group.monitoring_lock_sha256===current.monitoring_lock_sha256);
        const coverageRows=groups.flatMap(group=>Object.entries(group.source_strata||{}).map(([source,value])=>{
          const metricText=metric=>metric?.n?`${metric.covered_n??0}/${metric.n} · ${metric.empirical_coverage==null?'포함률 미산출':`${(Number(metric.empirical_coverage)*100).toFixed(1)}%`}`:'0건 · 포함률 미산출';
          return `<tr><td>${escapeHTML({user_reported_primary:'직접 일지 · 주 지표',automatic_wearable_auxiliary:'웨어러블 · 보조 지표'}[source]||source)}</td><td>${metricText(value.onset)}</td><td>${metricText(value.wake)}</td><td>${metricText(value.joint)}</td></tr>`;
        })).join('');
        evidenceTarget.innerHTML=`<strong>${escapeHTML(statusLabels[intervals.status]||intervals.status||'상태 미표시')} · 검증 완료 아님</strong><br>모니터링 단계 ${escapeHTML(current.monitoring_phase||'미표시')} · 보정 ${intervals.calibration_n??0}밤 · 명목 공동 포함률 ${Number.isFinite(nominal)?`${(nominal*100).toFixed(1)}% (보장 아님)`:'미설정'}
          ${intervalRows?`<div class="table-scroll"><table><thead><tr><th>모델</th><th>입면 보정 구간</th><th>기상 보정 구간</th><th>공동 반경</th></tr></thead><tbody>${intervalRows}</tbody></table></div><p class="muted small">표시 구간은 미래 보정 산출물이며 개인 정확도가 검증됐거나 포함률이 보장된 구간이 아닙니다.</p>`:'<p class="muted small">아직 표시할 보정 구간이 없습니다.</p>'}
          ${coverageRows?`<div class="table-scroll"><table><thead><tr><th>별도 미래 보류 결과</th><th>입면 포함</th><th>기상 포함</th><th>공동 포함</th></tr></thead><tbody>${coverageRows}</tbody></table></div><p class="muted small">전향 구간 포함률 관측은 시계열 의존성이 있는 기술통계이며 검증 완료나 보장된 정확도가 아닙니다.</p>`:'<p class="muted small">별도 미래 보류 결과 0건 · 전향 포함률 관측 대기.</p>'}`;
      }
      return `${warnings.map(value=>`<p>· ${escapeHTML(value)}</p>`).join('')}<p>· 일정은 수면 가능 시간 제약에 사용되는 미검증 후보입니다. 낮잠·카페인은 새 입력 특성으로 저장하며, 아직 개인별 효과 계수를 학습했다는 뜻은 아닙니다.</p>`;
    })();
}
function habitValue(value,suffix=''){
  return value==null?'—':`${Number(value).toFixed(1)}${suffix}`;
}
function renderHabitOutcomes(outcomes,effects){
  const target=$('habit-outcomes');if(!target)return;
  const effectPanel=()=>{
    if(!effects?.available)return `<section class="recommendation-effect-summary"><h3>추천 노출 후 후속 결과</h3><p>${escapeHTML(effects?.reason||'현재 가입 기간의 추천 노출 후속 결과가 없습니다.')}</p><p class="muted small">인과 효과 연구에는 별도 동의, 사전 선언한 배정, 미래 후속 관측이 필요하며 현재 운영 중인 무작위 시험은 없습니다.</p></section>`;
    const counts=effects.exposure_counts||{},follow=effects.descriptive_follow_up||{},subjective=follow.subjective_outcomes||{},timing=follow.survey_timing||{};
    const deviation=follow.absolute_bedtime_deviation_min||{};
    const subjectiveRow=(label,key)=>{
      const value=subjective[key]||{},responses=value.response_count??0,missing=value.missing_count??0;
      const result=value.scales_comparable===false?'척도별 분리 · 평균 미통합':habitValue(value.mean);
      return `<tr><td>${label}</td><td>${result}</td><td>${responses}건</td><td>${missing}건</td></tr>`;
    };
    return `<section class="recommendation-effect-summary"><h3>추천 노출 후 후속 결과 · 비인과 기술통계</h3>
      <p>같은 가입 기간의 일지 ${counts.total_eligible_records??0}건 중 <strong>실제 취침 전에 화면을 본 기록 ${counts.viewed_before_sleep??0}건</strong>만 노출 후 결과로 연결했습니다.</p>
      <p class="muted small">취침 이후 확인 ${counts.viewed_late_or_at_sleep??0}건 · 생성됐지만 미확인 ${counts.generated_but_unseen??0}건 · 노출시각 확인 불가 ${counts.exposure_timing_unknown??0}건 · 연결 추천 없음 ${counts.no_matching_recommendation??0}건. 생성만 된 추천은 준수로 간주하지 않습니다.</p>
      <div class="table-scroll"><table><thead><tr><th>노출 후 기술 지표</th><th>평균</th><th>응답</th><th>미응답</th></tr></thead><tbody>
      <tr><td>권장 취침과 절대 차이</td><td>${habitValue(deviation.mean,'분')}</td><td>${deviation.response_count??0}건</td><td>—</td></tr>
      ${subjectiveRow('개운함','rested')}${subjectiveRow('피로도 (0~10과 1~5 척도 미통합)','fatigue')}
      ${subjectiveRow('졸림 (사용자 정의·미검증)','sleepiness')}${subjectiveRow('응답 부담 (사용자 정의·미검증)','burden')}
      </tbody></table></div>
      <p>설문 실제 응답시각: 입력 확인 ${timing.known_count??0}건 · 미입력/확인 불가 ${timing.unknown_count??0}건. 기상이나 수신 시각으로 대체하지 않습니다.</p>
      <p><strong>인과 효과 추정: 없음 (관찰 기술통계만)</strong></p>
      <p class="muted small">추천 후속 결과와 사전 고정 예측 정확도는 별개입니다. ${escapeHTML(effects.forecast_accuracy?.reason||'개인 예측구간은 여기서 검증하지 않습니다.')}</p>
      <p class="muted small">효과 연구 전제: 별도 동의 · 사전 선언한 배정 · 이후 미래 추적. 현재 운영 중인 무작위 시험이나 인과 집단효과·p값은 없습니다.</p></section>`;
  };
  if(!outcomes?.available){target.innerHTML=`<p>${escapeHTML(outcomes?.reason||'가입 후 수면 습관 결과를 아직 계산할 수 없습니다.')}</p>${effectPanel()}`;return;}
  const primary=outcomes.primary||{},first=primary.first_14_days||{},recent=primary.recent_14_days||{};
  const row=(label,key,suffix='')=>`<tr><td>${label}</td><td>${habitValue(first[key],suffix)}</td><td>${habitValue(recent[key],suffix)}</td></tr>`;
  const aux=outcomes.auxiliary||{},auxFirst=aux.first_14_days||{},auxRecent=aux.recent_14_days||{};
  const compareNote=outcomes.comparison_available
    ?'두 기간의 기술통계만 표시합니다. 차이가 SleepWell의 효과이거나 통계적으로 유의하다는 뜻은 아닙니다.'
    :escapeHTML(outcomes.reason||'28일 이상 관측 후 겹치지 않는 두 기간을 비교합니다.');
  const scaleNote=first.fatigue_scales_comparable===false||recent.fatigue_scales_comparable===false?'피로도 0~10과 1~5 등 계약이 다른 척도는 평균으로 합치지 않고 척도별 응답수만 보존합니다.':'';
  target.innerHTML=`<p><strong>연구 시작일 ${escapeHTML(outcomes.enrollment_date)}</strong> · 관측 ${outcomes.elapsed_days}일</p>
    <p>${escapeHTML(primary.role||'가입 후 직접 기록을 주 결과로 사용합니다.')}</p>
    <div class="table-scroll"><table><thead><tr><th>주 결과</th><th>첫 14일</th><th>최근 14일</th></tr></thead><tbody>
    <tr><td>직접 기록 범위</td><td>${first.recorded_days??0}/14일</td><td>${recent.recorded_days??0}/14일</td></tr>
    ${row('취침시각 규칙성 (원형 표준편차)','bedtime_circular_sd_min','분')}
    ${row('기상시각 규칙성 (원형 표준편차)','wake_circular_sd_min','분')}
    ${row('아침 개운함 평균 (1~5)','rested_mean')}${row('피로도 평균 (동일 척도만)','fatigue_mean')}
    <tr><td>개운함 미응답</td><td>${first.missing_rested_days??14}일</td><td>${recent.missing_rested_days??14}일</td></tr>
    <tr><td>피로도 미응답</td><td>${first.missing_fatigue_days??14}일</td><td>${recent.missing_fatigue_days??14}일</td></tr>
    </tbody></table></div><p class="muted small">${compareNote} ${scaleNote}</p>
    <details class="outcome-details"><summary>웨어러블 보조 지표</summary><p>${escapeHTML(aux.role||'착용 상태의 영향을 받는 보조 지표입니다.')}</p>
    <div class="table-scroll"><table><thead><tr><th>보조 수면시각</th><th>첫 14일</th><th>최근 14일</th></tr></thead><tbody>
    <tr><td>자동 기록 범위</td><td>${auxFirst.recorded_days??0}/14일</td><td>${auxRecent.recorded_days??0}/14일</td></tr>
    <tr><td>취침시각 원형 표준편차</td><td>${habitValue(auxFirst.bedtime_circular_sd_min,'분')}</td><td>${habitValue(auxRecent.bedtime_circular_sd_min,'분')}</td></tr>
    <tr><td>기상시각 원형 표준편차</td><td>${habitValue(auxFirst.wake_circular_sd_min,'분')}</td><td>${habitValue(auxRecent.wake_circular_sd_min,'분')}</td></tr>
    </tbody></table></div><p class="muted small">웨어러블에서 개운함이나 피로도를 추정하지 않습니다. 가입 전·archive 자료도 결과에서 제외합니다.</p></details>
    <p class="muted small">${escapeHTML(outcomes.sleep_pressure?.note||'수면압력 S는 직접 측정값이 아니라 모델 계산값입니다.')}</p>${effectPanel()}`;
}
function flattenResearchMetrics(fusion){
  if(!fusion?.research_metrics)return fusion?.test_metrics||{};
  return Object.fromEntries(Object.entries(fusion.research_metrics).flatMap(([cohort,value])=>
    Object.entries(value.test||{}).map(([arm,metrics])=>[`${arm}_${cohort}`,metrics])));
}
function renderFusionStatus(fusion){
  const target=$('fusion-status');if(!target)return;
  if(!fusion?.available){target.innerHTML=`<p>${escapeHTML(fusion?.reason||'배포된 CMI·MESA 융합 모델을 사용할 수 없습니다.')}</p>`;return;}
  const metrics=flattenResearchMetrics(fusion);
  const rows=Object.entries(metrics).map(([arm,m])=>`<tr><td>${escapeHTML(forecastLabels[arm]||arm)}</td><td>${m?.n_nights??m?.n??m?.test_n??'—'}</td><td>${metric(m?.onset_mae_min)}</td><td>${metric(m?.wake_mae_min)}</td><td>${metric(m?.joint_mae_min)}</td></tr>`).join('');
  const artifacts=JSON.stringify(fusion.artifacts||fusion.cohorts||{});
  target.innerHTML=`<p><strong>배포 모델 ${escapeHTML(fusion.runtime_model_version||fusion.model_version||'버전 미표시')}</strong></p>
    ${fusion.research_metrics_scope==='frozen_legacy14_artifact_metrics'?'<p class="muted small">아래는 기존 legacy14 정책의 보존된 Test 결과입니다. 첫 기록부터 적용하는 현재 gradual_v1 정책의 검증 성능이 아닙니다.</p>':''}
    ${rows?`<div class="table-scroll"><table><thead><tr><th>연구 기준·모델</th><th>평가 밤</th><th>시작 MAE</th><th>기상 MAE</th><th>평균 MAE</th></tr></thead><tbody>${rows}</tbody></table></div>`:''}
    <p class="muted small">CMI와 MESA 결과를 각각 표시하며 특정 모델을 승자로 단정하지 않습니다. 연구 집단 오차는 현재 사용자의 정확도나 수면 개선 효과가 아닙니다.</p>
    <details class="outcome-details"><summary>배포 산출물</summary><p class="muted small">${escapeHTML(artifacts)}</p></details>`;
}
function notice(message, error=false) { $('notice').textContent=message; $('notice').hidden=false; $('notice').className=error?'error':''; }
async function request(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-SleepWell-Token':state.csrf_token},body:JSON.stringify(data)};
  const response=await fetch(path,options); const result=await response.json();
  if(!response.ok) throw new Error(result.error || '요청을 완료하지 못했습니다.'); return result;
}
function setView(view) {
  if(!state?.profile && !['settings','research','phone-research','overview','participants','lab','integrations'].includes(view)) view='settings';
  currentView=view;
  document.querySelectorAll('.view').forEach(e=>e.hidden=true);
  $(view==='settings'?'onboarding':view).hidden=false;
  document.querySelectorAll('.nav').forEach(e=>e.classList.toggle('active',e.dataset.view===view));
  if(view==='dashboard') requestAnimationFrame(()=>{drawChart();markRecommendationViewed();});
  document.dispatchEvent(new CustomEvent('sleepwell:view',{detail:{view}}));
}
function markRecommendationViewed(){
  const id=state?.recommendation?.id;
  if(currentView!=='dashboard'||document.visibilityState!=='visible'||!id||state?.recommendation?.stale_policy||viewedRecommendations.has(id))return;
  viewedRecommendations.add(id);
  request('/api/recommendation-viewed',{recommendation_id:id}).catch(()=>viewedRecommendations.delete(id));
}
document.addEventListener('visibilitychange',markRecommendationViewed);
function profileForm() {
  const p=state.profile; if(!p) return;
  const form=$('profile-form');
  for(const [key,value] of Object.entries(p)) { const input=form.elements.namedItem(key); if(!input)continue; if(input.type==='checkbox')input.checked=value;else input.value=value; }
  form.querySelector('[type=submit]').textContent='시작 정보 수정하고 추천 갱신 →';
}
function render() {
  const s=state, p=s.profile, r=s.recommendation;
  const model=s.model_status;
  $('model-status').innerHTML=model?`<p><strong>${model.available?'연결 가능한 모델 있음':'현재 행동 예측 사용 불가'}</strong></p><p>${escapeHTML(model.reason)}</p>${model.model_version?`<p class="muted small">모델 버전: ${escapeHTML(model.model_version)}</p>`:''}`:'모델 연결 상태를 확인할 수 없습니다.';
  const mesa=s.data_readiness;
  if(mesa?.available) {
    const q=mesa.quality, modalities=mesa.modalities;
    const pct=v=>v==null?'검사값 없음':`${(v*100).toFixed(1)}%`;
    $('mesa-evidence').innerHTML=`<p><strong>${Number(mesa.file_count).toLocaleString()}개 파일 · ${(mesa.total_bytes/1e9).toFixed(2)} GB</strong></p><p>${escapeHTML(mesa.reason)} 점검 시각: ${escapeHTML(mesa.generated_at)}</p><dl><dt>활동계 CSV</dt><dd>${Number(modalities.actigraphy).toLocaleString()}개</dd><dt>PSG 원시 기록 / 단계 주석</dt><dd>${modalities.psg_edf} / ${modalities.psg_annotation}개</dd><dt>심박 / 걸음 파일</dt><dd>${modalities.heart_rate} / ${modalities.steps}개</dd><dt>내용 검사 범위</dt><dd>안정된 표본 ${q.sampled_files}개 · ${Number(q.rows).toLocaleString()}행</dd><dt>표본 비착용 플래그 비율</dt><dd>${pct(q.offwrist_fraction)}</dd><dt>표본 30초 간격 비율</dt><dd>${pct(q.thirty_second_cadence_fraction)}</dd></dl><p>활동·빛·수면 패턴 연구의 입력 후보입니다. ACT 수면·각성 표시는 장치에서 파생된 값이며 PSG 수면단계 정답과 다릅니다.</p><p class="muted small">표본 검사 비율을 전체 데이터의 품질로 일반화하지 않습니다. 이 집계는 실시간 다운로드 진행률이 아니며, 추가 업로드 후 점검 파일을 갱신하고 서버를 재시작해야 합니다.</p>`;
  } else {
    $('mesa-evidence').textContent=mesa?.reason || '아직 MESA 데이터 점검 결과가 연결되지 않았습니다.';
  }
  renderHabitOutcomes(s.habit_outcomes,s.recommendation_effects);
  renderFusionStatus(s.fusion_status);
  $('archive-intro').textContent=s.research.available ? `연결 가능한 참여자 가입 전 보조 자료: ${s.research.sleep_nights}일 수면 · 활동 ${s.research.step_days}일. ${s.research.first_date} ~ ${s.research.last_date}. 가입 후 연구 결과와 모델 선택에는 포함하지 않습니다.` : '가입 전 자료 연결 없이도 참여자 설정으로 시작할 수 있습니다.';
  $('use-archive').disabled=!s.research.available;
  if(p) {
    $('greeting').textContent=`${p.name} 참여자의 수면 리듬 연구.`;
    $('subtitle').textContent='가입 후 직접 기록을 주 결과로, 자동 수면시각을 보조 지표로 추적합니다.';
  }
  if(r) {
    $('bed-time').textContent=clock(r.bed); $('wake-time').textContent=clock(r.wake);
    $('bed-date').textContent=r.stale_policy?`이전 정책 · ${day(r.bed)}`:day(r.bed); $('wake-date').textContent=r.stale_policy?`이전 정책 · ${day(r.wake)}`:day(r.wake);
    $('plan-date').textContent=r.stale_policy?`이전 정책 역사 기록 · ${day(r.generated_at)} 생성`:`${day(r.generated_at)} 생성`;
    $('sleep-opportunity').textContent=`실행 가능한 잠자리 기회 ${duration(r.sleep_opportunity_min)}`;
    $('wind-down').textContent=`${clock(r.wind_down)}부터 잠들 준비`;
    const q=s.personalization;
    $('valid-days').textContent=q.valid_days;
    $('ring-fill').style.strokeDashoffset=String(314.16*(1-Math.max(0,Math.min(1,Number(q.personalization_weight)||0))));
    const effective=q.effective_records!=null&&Number.isFinite(Number(q.effective_records))?Number(q.effective_records):q.valid_days;
    const gap=q.last_record_gap_days!=null&&Number.isFinite(Number(q.last_record_gap_days))?Number(q.last_record_gap_days):null;
    const halfLife=q.recency_half_life_days!=null&&Number.isFinite(Number(q.recency_half_life_days))?Number(q.recency_half_life_days):null;
    $('personalization-title').textContent=q.mode==='personalized'?'최근 기록으로 매일 갱신 중':'첫 기록을 기다리는 중';
    const warm=q.population_prior;
    const warmText=warm?.available?`${warm.level} · ${Number(warm.n_people).toLocaleString()}명 · ${duration(warm.mean_sleep_min)} 관측 평균 (설명용, 목표 미사용)`:(warm?.reason||'집단 기술자료 없음');
    $('growth-description').textContent=q.mode==='personalized'?`누적 ${q.cumulative_valid_days}일 중 최근성 유효 기록 ${effective.toFixed(1)}일을 일정 계산에 반영합니다. 마지막 기록 공백 ${gap==null?'없음':`${gap.toFixed(1)}일`} · 개인 기록 가중치 ${(q.personalization_weight*100).toFixed(0)}%. ${evidenceQualityText(q)}`:`추천은 가입 정보와 사용자 요청 목표로 계산합니다. ${warmText}은 기술 비교용이며 목표에 사용하지 않습니다. ${evidenceQualityText(q)}`;
    $('target-duration').textContent=duration(Number(p.desired_sleep_h)*60);
    {
          $('observed-duration').textContent=sleepEstimate(r.observed_sleep_min);
          $('goal-difference').textContent=goalDifferenceText(r);
        };
    $('activity-baseline').textContent=`${Math.round(q.average_steps).toLocaleString()}보`;
    $('activity-origin').textContent=q.origins.activity;
    $('prediction-count').textContent=`${s.prediction_count}회`;
    $('reasons').innerHTML=(r.reasons||[]).map(x=>`<li>${escapeHTML(nonBiologicalDifferenceWording(x))}</li>`).join('');
    const warnings=[...(r.warnings||[])];
    {
          const staleMessage='이 추천은 이전 정책으로 계산된 역사 기록입니다. 현재 수정 정책의 계산 결과가 아니므로 반드시 현재 기록으로 다시 계산해야 합니다.';
          $('stale-policy-warning').hidden=!r.stale_policy;
          $('stale-policy-warning').textContent=r.stale_policy?staleMessage:'';
          $('recommendation-heading').textContent=r.stale_policy?'이전 정책의 역사 기록입니다.':'오늘은 이 리듬으로 하루를 마무리해 보세요.';
          if(r.stale_policy)warnings.unshift(staleMessage);
          if(r.plan_date!==s.today)warnings.unshift('이 일정은 이전 날짜에 만든 추천입니다. 현재 기록으로 다시 계산해주세요.');
        }
    $('warnings').innerHTML=warnings.map(x=>`<p>· ${escapeHTML(nonBiologicalDifferenceWording(x))}</p>`).join('');
    const pattern=q.weektype_pattern||{};
    const patternText=pattern.status==='exploratory_sufficient'?`평일 ${pattern.weekday_count}일 / 주말 ${pattern.weekend_count}일 · 주말 중간시각 차이 ${Number(pattern.weekend_minus_weekday_min).toFixed(0)}분 (탐색값, 추천 미적용)`:`평일 ${pattern.weekday_count||0}일 / 주말 ${pattern.weekend_count||0}일 · 각 2일 이상 전까지 패턴 판단 보류`;
    $('parameter-details').innerHTML=`<dl><dt>개인화 모드</dt><dd>${q.mode==='initial'?'가입 정보 기준':'첫 기록부터 연속 갱신'}</dd><dt>개인 기록 가중치</dt><dd>${(q.personalization_weight*100).toFixed(0)}% · 누적 ${q.cumulative_valid_days}일 (확률 아님)</dd><dt>최근성 유효 기록</dt><dd>${effective.toFixed(1)}일${halfLife==null?'':` · 반감기 ${halfLife}일`}</dd><dt>마지막 기록 공백</dt><dd>${gap==null?'기록 없음':`${gap}일`}</dd><dt>근거 품질 플래그</dt><dd>${escapeHTML(evidenceQualityText(q))}</dd><dt>관측 가능 시각 미상 제외</dt><dd>${q.unknown_observation_time_excluded_count??0}건</dd><dt>평일·주말 패턴</dt><dd>${escapeHTML(patternText)}</dd><dt>행동 수면 위상</dt><dd>${Math.floor(q.phase_min/60)}시 ${Math.round(q.phase_min%60)}분 · ${escapeHTML(q.phase_status||'상태 미표시')} · resultant ${q.phase_resultant_length==null?'—':Number(q.phase_resultant_length).toFixed(3)}</dd><dt>운동 기준</dt><dd>${q.exercise_min.toFixed(1)}분/일 · ${escapeHTML(q.origins.exercise)}</dd><dt>각성·수면 시간상수</dt><dd>${q.tau_wake_h}시간 / ${q.tau_sleep_h}시간 · 현재 고정값</dd><dt>사용자 요청 목표</dt><dd>${duration(Number(p.desired_sleep_h)*60)} · 측정된 필요량 아님</dd><dt>개인화 계산 목표</dt><dd>${duration((r.parameters?.target_sleep_min??q.target_sleep_min))} · ${escapeHTML(q.origins.sleep_target)}</dd><dt>오늘 계산 목표</dt><dd>${duration(r.target_sleep_min)} · 적용된 연장 포함</dd><dt>최근 측정 추정치</dt><dd>${sleepEstimate(r.observed_sleep_min)}</dd><dt>실행 가능한 잠자리 기회</dt><dd>${duration(r.sleep_opportunity_min)}</dd><dt>집단 관측 평균</dt><dd>${escapeHTML(warmText)}${warm?.version?` · ${escapeHTML(warm.version)}`:''}</dd><dt>권장시각 예측구간</dt><dd>${q.uncertainty?.prediction_interval_status==='unvalidated'?'미검증 · 표시하지 않음':escapeHTML(q.uncertainty?.prediction_interval_status||'정보 없음')}</dd></dl><p>${escapeHTML(q.note)}</p>`;
    const b=r.behavioral_forecast;
    $('forecast-details').innerHTML=b?.available?`<p><strong>기존 연구 모델 연결:</strong> 예상 행동 ${clock(b.bed)} 취침 / ${clock(b.wake)} 기상<br>${escapeHTML(b.role)}<br>${escapeHTML(b.warning)}</p>`:`<p>기존 행동 예측 모델: ${escapeHTML(b?.reason || '미연결')}</p>`;
    const next=r.fusion_forecast||r.next_night_forecast;
    if(next?.available){
      const rows=Object.entries(next.predictions).map(([arm,p])=>{
        const m=flattenResearchMetrics(next)[arm];
        return `<tr><td>${escapeHTML(forecastLabels[arm]||arm)}</td><td>${clock(p.bed)}</td><td>${clock(p.wake)}</td><td>${duration(p.interval_min)}</td><td>${m&&Number.isFinite(Number(m.onset_mae_min))&&Number.isFinite(Number(m.wake_mae_min))?`${Number(m.onset_mae_min).toFixed(1)} / ${Number(m.wake_mae_min).toFixed(1)}분`:'—'}</td></tr>`;
      }).join('');
      const personalState=next.personalization?.cmi??next.personalization;
      const validDays=personalState?.valid_days;
      const personalWeight=personalState?.personal_weight;
      $('next-night-details').innerHTML=`<p>${next.cutoff?`${day(next.cutoff)} 20:00 기준 · `:''}${escapeHTML(next.source||'CMI·MESA 연구 기준')}</p><div class="table-scroll"><table><thead><tr><th>연구 기준·모델</th><th>수면 시작 예상</th><th>기상 예상</th><th>시작~기상 간격 (불확실성 구간 아님)</th><th>연구 평가 MAE (시작 / 기상)</th></tr></thead><tbody>${rows}</tbody></table></div><p>${escapeHTML(next.warning||'')} ${escapeHTML(next.population_scope||'')}</p><p class="muted small">${validDays==null?'':`가입 후 개인 기록 ${validDays}일. `}${personalWeight==null?'':`장기 보정 비중 ${(personalWeight*100).toFixed(0)}%. `}표시된 MAE는 연구 평가 집단의 평균 오차이며 오늘 개인 예측의 검증된 구간이나 보장 범위가 아닙니다. 개인 Samsung 과거 자료는 배포 모델 선택에 사용하지 않습니다.</p>`;
    }else{
      $('next-night-details').textContent=next?.reason||'현재 저장된 일정에는 새 예측이 없습니다. 현재 기록으로 다시 계산하면 표시됩니다.';
    }
    drawChart();
  }
  if(p&&contextLoadedNight===null)populateContext(s.daily_context,s.context_night_id||s.today);
  renderProspective(s.prospective,s.context_night_id||s.today);
  const diary=s.records.filter(x=>x.source!=='archive');
  $('history-summary').textContent=`새로 기록한 수면 ${diary.length}일 · 연결된 과거 수면 ${s.records.length-diary.length}일 · 저장된 추천 ${s.prediction_count}개`;
  const feedback=new Map(s.feedback.map(x=>[x.date,x]));
  $('history-rows').innerHTML=[...s.records].reverse().map(x=>{
    const f=feedback.get(x.date), delta=f?.exposure_before_sleep===true?f.bed_difference_min:null;
    const editable=x.source!=='archive';
    return `<tr><td>${escapeHTML(x.date)}</td><td>${x.time_in_bed_min==null?'—':duration(x.time_in_bed_min)}</td><td>${sleepEstimate(x.sleep_min)}</td><td>${x.rested??'—'}</td><td>${x.fatigue??'—'}</td><td>${delta==null?(f?.recommendation_id?'취침 전 노출 미확인':'사전 추천 없음'):`${delta>=0?'+':''}${Math.round(delta)}분`}</td><td>${x.source==='archive'?'과거 Watch':x.source==='healthkit'?'Apple Health':x.source==='health_connect'?'삼성헬스 연동':'직접 기록'}</td><td>${editable?`<button class="table-edit" type="button" data-edit-record="${escapeHTML(x.date)}">수정</button>`:'—'}</td></tr>`;
  }).join('') || '<tr><td colspan="8">첫 수면 기록을 남겨주세요.</td></tr>';
  const m=s.model_metrics;
  $('model-evidence').innerHTML=m?`<p>기존 행동 예측 실험: 총 ${m.n_total}일, 테스트 ${m.wake.test_n}일. 취침 MAE ${m.bedtime.test_mae_minutes.toFixed(1)}분 · 기상 MAE ${m.wake.test_mae_minutes.toFixed(1)}분.</p><p>이 수치는 과거 행동을 맞힌 오차입니다. 현재 권장 일정의 수면 개선 효과를 평가한 결과는 아직 없습니다. 목표는 개운함·피로도·수면 규칙성의 장기 변화로 검증합니다.</p>`:'기존 연구 모델 평가 파일을 찾지 못했습니다.';
  $('save-status').textContent='기록은 브라우저가 아닌 로컬 DB에 저장됩니다';
  setView(currentView);
}
function drawChart() {
  const canvas=$('rhythm-chart'), points=state?.recommendation?.chart;
  if(!canvas||!points||canvas.clientWidth===0)return;
  const ratio=window.devicePixelRatio||1, w=canvas.clientWidth,h=210;
  canvas.width=w*ratio;canvas.height=h*ratio;
  const c=canvas.getContext('2d');c.scale(ratio,ratio);c.clearRect(0,0,w,h);
  const left=34,right=12,top=16,bottom=30;
  const x=i=>left+i/(points.length-1)*(w-left-right);
  const y=value=>top+(1.05-value)/1.25*(h-top-bottom);
  c.font='10px -apple-system, sans-serif';
  for(const v of [0,.25,.5,.75,1]) {c.strokeStyle='#efedf4';c.beginPath();c.moveTo(left,y(v));c.lineTo(w-right,y(v));c.stroke();c.fillStyle='#b1aaba';c.fillText(String(v),5,y(v)+3);}
  for(const [key,color] of [['s','#8e79c5'],['c','#a8b9c9']]){c.strokeStyle=color;c.lineWidth=2;c.beginPath();points.forEach((p,i)=>i?c.lineTo(x(i),y(p[key])):c.moveTo(x(i),y(p[key])));c.stroke();}
  c.fillStyle='#a49bad';for(let i=0;i<points.length;i+=36){c.fillText(clock(points[i].time),Math.min(w-35,Math.max(left-10,x(i)-14)),h-6);}
}
async function save(path,data,message) {
  const buttons=[...document.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
  try {state=await request(path,data);currentView='dashboard';render();notice(message);window.scrollTo({top:0,behavior:'smooth'});return true;}
  catch(e){notice(e.message,true);return false;}
  finally{buttons.forEach(b=>b.disabled=false);$('use-archive').disabled=!state.research.available;updateAddButtons();}
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{if(button.dataset.view==='settings')profileForm();setView(button.dataset.view);}));
$('profile-form').addEventListener('submit',event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.target));data.link_history=event.target.elements.link_history.checked;save('/api/profile',data,'시작 정보를 저장하고 수면 일정을 만들었습니다.');});
$('record-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.target));if(!data.replaces_date)delete data.replaces_date;const saved=await save('/api/record',data,'수면 기록과 피드백을 저장했습니다. 오늘의 추천도 갱신했습니다.');if(saved)clearRecordEdit(true);});
$('cancel-record-edit').addEventListener('click',()=>{clearRecordEdit(true);notice('기록 수정을 취소했습니다. 새 기록 입력 상태로 돌아왔습니다.');});
$('history-rows').addEventListener('click',event=>{const button=event.target.closest('[data-edit-record]');if(button)startRecordEdit(button.dataset.editRecord);});
$('regenerate').addEventListener('click',()=>save('/api/recommend',{},'새 추천을 기록했습니다. 기존 추천 이력도 보존됩니다.'));
$('use-archive').addEventListener('click',()=>{const average=state.research.average_steps;if(average!=null){$('profile-form').elements.average_steps.value=Math.round(average);$('profile-form').elements.link_history.checked=true;notice('본인의 과거 걸음 평균을 채웠습니다. 다른 생활 정보는 현재 기준으로 입력해주세요.');}});
$('add-schedule').addEventListener('click',()=>addRepeatRow('schedule'));
$('add-nap').addEventListener('click',()=>addRepeatRow('nap'));
$('add-caffeine').addEventListener('click',()=>addRepeatRow('caffeine'));
$('context-form').addEventListener('input',()=>{contextDirty=true;});
$('context-night-id').addEventListener('change',async event=>{
  const nightId=event.target.value;if(!nightId)return;
  $('context-save-state').textContent='불러오는 중…';
  try{const result=await request(`/api/context?night_id=${encodeURIComponent(nightId)}`);populateContext(result.context,result.night_id||nightId);}
  catch(error){$('context-save-state').textContent='불러오기 실패';notice(error.message,true);}
});
$('context-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.target;
  const schedules=readRows('schedule'),naps=readRows('nap'),caffeine=readRows('caffeine');
  const invalidSchedule=schedules.some(item=>item.end&&item.end<=item.start);
  const invalidNap=naps.some(item=>item.end<=item.start);
  if(invalidSchedule||invalidNap){notice('종료 시각은 시작 시각보다 늦게 입력해주세요.',true);return;}
  const payload={night_id:form.elements.night_id.value,schedules,preparation_min:Number(form.elements.preparation_min.value),naps,caffeine};
  const button=form.querySelector('[type=submit]');button.disabled=true;$('context-save-state').textContent='저장 중…';
  try{
    await request('/api/context',payload);
    state=await request('/api/state');contextDirty=false;contextLoadedNight=null;render();
    notice('다음날 일정과 오늘의 생활 정보를 저장했습니다. 이미 고정된 사전 예측은 변경되지 않습니다.');
  }catch(error){$('context-save-state').textContent='저장 실패';notice(error.message,true);}
  finally{button.disabled=false;updateAddButtons();}
});
window.addEventListener('resize',drawChart);
async function foregroundRefresh(){
  if(document.visibilityState!=='visible'||!state)return;
  const timing=state.recommendation_schedule, now=new Date();
  const initial=timing?new Date(timing.initial_decision_at):null;
  const cutoff=timing?new Date(timing.model_cutoff):null;
  const end=cutoff?new Date(cutoff.getTime()+4*60*60*1000):null;
  const eligible=state.profile&&initial&&end&&now>=initial&&now<end;
  state=await request(eligible?'/api/awake-observation':'/api/state',
    eligible?{state:'awake',source:'foreground_ui'}:undefined);
  render();
}
request('/api/state').then(async result=>{state=result;render();profileForm();setDefaultRecordTimes();await foregroundRefresh();}).catch(e=>notice(e.message,true));
setInterval(()=>{foregroundRefresh().catch(()=>{});},60000);
