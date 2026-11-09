/* Desktop-only local controls. No health data or secrets are sent to a cloud. */
(() => {
  const card = document.getElementById('mobile-card');
  const byId = id => document.getElementById(id);
  let busy = false;
  let expiry;
  function clearCode() {
    byId('mobile-code').value = '';
    byId('mobile-code-box').hidden = true;
    clearTimeout(expiry);
  }
  function render(s) {
    card.hidden = false;
    const select = byId('mobile-host');
    const previous = select.value;
    select.replaceChildren();
    for (const host of s.addresses) {
      const option = document.createElement('option');
      option.value = host; option.textContent = host;
      select.appendChild(option);
    }
    if (s.addresses.includes(previous)) select.value = previous;
    byId('mobile-start').disabled = busy || !s.addresses.length;
    byId('mobile-stop').disabled = busy || !s.enabled;
    byId('mobile-revoke').disabled = busy || !s.devices.length;
    byId('mobile-status').textContent = s.enabled
      ? `연결 수신 중 · 포트 ${s.port}. 앱을 종료하면 연결도 중지됩니다.`
      : '연결 중지 · 연결 시작을 누르면 선택한 내부 네트워크에서만 수신합니다.';
    if (!s.addresses.length) byId('mobile-status').textContent = '휴대폰과 같은 Wi-Fi 또는 내부 네트워크에 연결해주세요.';
    if (s.last_error) byId('mobile-status').textContent += ` 마지막 수신 오류: ${s.last_error.code} (${s.last_error.type}). 앱을 재시작하고 다시 동기화해주세요.`;
    if (s.notice) byId('mobile-status').textContent += ` ${s.notice}`;
    byId('mobile-device').textContent = s.devices.length
      ? s.devices.map(d => `${d.name} · 마지막 수신: ${d.last_sync ? new Date(d.last_sync).toLocaleString('ko-KR') : '아직 없음'}`).join('\n')
      : '등록된 휴대폰이 없습니다.';
    const info = s.import_summary || {};
    byId('mobile-import').textContent = `직접 입력과 충돌하여 보류: ${info.conflicts_pending || 0}일 · 수면 단계 부족·불명확 또는 짧은 수면으로 자동 활용에서 제외: ${info.records_held || 0}건. 보류 기록은 기존 기록을 덮어쓰지 않습니다.`;
    const contexts = s.context_summary || {};
    const kinds = contexts.by_kind || {};
    byId('mobile-import').textContent += ` 생활 컨텍스트: 걸음 ${kinds.steps || 0} · 운동 ${kinds.exercise || 0} · 카페인 ${kinds.caffeine || 0} · 일정 ${kinds.schedule || 0} · 낮잠 ${kinds.nap || 0}건.`;
    let backup = byId('mobile-backup-status');
    if (!backup) {
      backup = document.createElement('p'); backup.id = 'mobile-backup-status';
      card.appendChild(backup);
      const exportLink = document.createElement('a');
      exportLink.href = '/api/mobile/backups'; exportLink.download = 'SleepWell-phone-backups.json';
      exportLink.className = 'secondary'; exportLink.textContent = '최근 수신 백업 90건 JSON 저장';
      card.appendChild(exportLink);
      const note = document.createElement('p'); note.className = 'muted small';
      note.textContent = '휴대폰 백업은 참여자별로 별도 보관합니다. 수신 검사는 백업 형식·범위에 한하며, 휴대폰 계산 결과는 독립 검증 전 원본 스냅샷으로 보관합니다. PC의 현재 프로필·일지를 덮어쓰지 않습니다. 자동 전송은 PC 앱과 수신 연결이 켜져 있고 같은 내부 네트워크에서 연결 가능할 때 처리됩니다.';
      card.appendChild(note);
    }
    const backups = s.backup_summary || {};
    backup.textContent = `사용자 앱 백업: ${backups.participants || 0}개 참여자 식별 · ${backups.days || 0}일 · ${backups.versions || 0}개 버전. 마지막 수신: ${backups.last_received_at ? new Date(backups.last_received_at).toLocaleString('ko-KR') : '아직 없음'}`;
    if (!s.pairing_active) clearCode();
  }
  async function refresh() {
    try {
      const response = await fetch('/api/mobile/status');
      if (response.ok) render(await response.json());
    } catch { /* This panel is unavailable in the original development web host. */ }
  }
  async function action(path, body = {}) {
    if (busy) return;
    busy = true;
    for (const id of ['mobile-start', 'mobile-stop', 'mobile-revoke']) byId(id).disabled = true;
    byId('mobile-message').textContent = '처리 중…';
    try {
      const stateResponse = await fetch('/api/state');
      if (!stateResponse.ok) throw Error('앱 상태를 읽지 못했습니다.');
      const appState = await stateResponse.json();
      const response = await fetch(`/api/mobile/${path}`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-SleepWell-Token': appState.csrf_token},
        body: JSON.stringify(body)
      });
      const value = await response.json();
      if (!response.ok) throw Error(value.error || '연결 설정에 실패했습니다.');
      render(value);
      if (value.pairing_uri) {
        byId('mobile-code').value = value.pairing_uri;
        byId('mobile-code-box').hidden = false;
        clearTimeout(expiry);
        expiry = setTimeout(clearCode, value.expires_in_seconds * 1000);
      }
      byId('mobile-message').textContent = path === 'revoke'
        ? '휴대폰 접근을 해제했습니다. 기존에 받은 수면 기록은 유지됩니다.'
        : path === 'stop' ? '수신을 중지했습니다.' : '휴대폰 연결 앱에 코드를 붙여넣으세요. 코드는 10분 동안 한 번만 사용할 수 있습니다.';
    } catch (error) {
      byId('mobile-message').textContent = error.message;
    } finally { busy = false; await refresh(); }
  }
  byId('mobile-start').addEventListener('click', () => action('start', {host: byId('mobile-host').value}));
  byId('mobile-stop').addEventListener('click', () => action('stop'));
  byId('mobile-revoke').addEventListener('click', () => action('revoke'));
  byId('mobile-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(byId('mobile-code').value);
      byId('mobile-message').textContent = '연결 코드를 복사했습니다. 본인 휴대폰에만 전달해주세요.';
    } catch {
      byId('mobile-code').select();
      byId('mobile-message').textContent = '선택된 연결 코드를 복사해주세요.';
    }
  });
  refresh();
  setInterval(() => { if (!document.hidden && !busy) refresh(); }, 15000);
})();
