"""Read-only, identity-separated snapshots of the running desktop application."""
from collections import Counter
from datetime import timedelta
import hashlib
import json

from night_core import cutoff_for_onset
from prospective import known_onset_records
from research_lab_context import instant, normalize_context


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _source_id(device, participant):
    return 'phone-' + hashlib.sha256((device + '\0' + participant).encode()).hexdigest()[:16]


class AppLabBridge:
    def __init__(self, store, app_url, version):
        self.store, self.app_url, self.version = store, app_url, version

    def _sources(self):
        identities = set()
        with self.store.connect() as db:
            tables = _tables(db)
            if 'mobile_daily_backups' in tables:
                identities.update(db.execute('SELECT DISTINCT device_id,participant_key FROM mobile_daily_backups'))
            if 'mobile_context_events' in tables:
                identities.update(db.execute('SELECT DISTINCT device_id,participant_key FROM mobile_context_events'))
            if 'mobile_sleep_sessions' in tables:
                for (device,) in db.execute('SELECT DISTINCT device_id FROM mobile_sleep_sessions'):
                    if not any(d == device for d, _ in identities):
                        identities.add((device, ''))
        return {_source_id(d, p): (d, p) for d, p in sorted(identities)}

    def session(self):
        return {'connected': True, 'app_url': self.app_url, 'app_version': self.version,
                'sources': [{'id': 'pc', 'label': '본 앱 · 현재 PC 참가자'}] +
                           [{'id': key, 'label': f'연결 휴대폰 · {key[6:14]}'} for key in self._sources()]}

    def snapshot(self, source='pc'):
        sources = self._sources()
        if source != 'pc' and source not in sources:
            raise ValueError('현재 연결된 기록 출처를 선택해주세요.')
        participant = 'pc' if source == 'pc' else source
        warnings = ['현재 저장된 기록의 후향 재생입니다. 원래 전향 예측의 성능을 뜻하지 않습니다.',
                    '걸음·운동·카페인·일정은 조건 분석용입니다. 동결 모델의 입력 변수는 바뀌지 않습니다.',
                    '활동 합계는 수신한 구간만 집계하며 하루 전체 수집을 보장하지 않습니다.']
        candidates, events, context_provenance = [], [], []
        skipped = 0
        if source == 'pc':
            profile = self.store.profile() or {}
            enrollment = profile.get('enrollment_id')
            for raw in self.store.records():
                if raw.get('provenance', {}).get('device_id'):
                    continue
                if not enrollment or raw.get('enrollment_id') != enrollment:
                    continue
                candidates.append((raw, raw.get('observed_at'), 1))
            # A context snapshot is a replacement, not an additive event stream.
            latest = {}
            for context in self.store.contexts():
                if not enrollment or context.get('enrollment_id') != enrollment:
                    skipped += 1
                    continue
                latest[context['night_id']] = context
            for context in latest.values():
                events.extend(_pc_context(context, participant))
            warnings.append('PC 생활 정보는 야간별 최신 수정본입니다. 수정 전 이력은 복원하지 않으며 늦게 수정된 값은 해당 20시 집계에서 제외합니다.')
            warnings.append('현재 PC 참가자에게 귀속됨을 확인할 수 없는 과거 기록은 제외합니다.')
            if any(r[0].get('steps') is not None or r[0].get('exercise_min') is not None for r in candidates):
                warnings.append('기존 수면 일지의 걸음·운동 총량은 활동 시각이 없어 20시 이전 활동으로 추정하지 않았습니다.')
        else:
            device, key = sources[source]
            with self.store.connect() as db:
                tables = _tables(db)
                backups = []
                if 'mobile_daily_backups' in tables:
                    backups = db.execute('SELECT received_at,payload FROM mobile_daily_backups WHERE device_id=? AND participant_key=? ORDER BY received_at,day,revision', (device, key)).fetchall()
                # Latest snapshot controls deletions; identical versions retain first receipt.
                first_seen = {}
                latest = None
                for received, payload in backups:
                    latest = json.loads(payload)
                    for raw in latest['data']['records']:
                        digest = json.dumps(raw, sort_keys=True, separators=(',', ':'))
                        first_seen.setdefault(digest, received)
                if latest:
                    for raw in latest['data']['records']:
                        if raw.get('source') != 'manual':
                            continue  # HC session boundaries are not an onset label.
                        observed = first_seen[json.dumps(raw, sort_keys=True, separators=(',', ':'))]
                        candidates.append(({'sleep_start': raw.get('bedtime'), 'wake': raw.get('wake_time')}, observed, 2))
                # Device-only sleep data may be assigned only if identity is unambiguous.
                if 'mobile_sleep_sessions' in tables and sum(d == device for d, _ in sources.values()) == 1:
                    from mobile_store import _record_from_session
                    for sid, observed, payload in db.execute('SELECT session_id,observed_at,data FROM mobile_sleep_sessions WHERE device_id=? AND deleted=0', (device,)):
                        raw = dict(json.loads(payload), device_id=device, session_id=sid)
                        record = _record_from_session(raw)
                        if record:
                            candidates.append((record, observed, 1))
                elif 'mobile_sleep_sessions' in tables:
                    warnings.append('같은 기기에 여러 참가자 이력이 있어 참가자를 구분할 수 없는 센서 수면은 제외했습니다.')
            from mobile_context_store import list_events
            for raw in list_events(self.store, device_id=device, participant_key=key):
                if raw.get('participant_key', '') != key:
                    continue
                context_provenance.append({
                    'participant_id': participant,
                    **{field: raw.get(field) for field in (
                        'event_id', 'group_id', 'kind', 'occurrence_start',
                        'occurrence_end', 'value', 'source_modified_at',
                        'device_observed_at', 'version_received_at',
                        'last_received_at', 'available_at', 'source', 'details',
                    )},
                })
                if raw.get('available_at') is None:
                    skipped += 1
                    continue
                events.append({
                    'participant_id': participant,
                    'kind': raw.get('kind'),
                    'start': raw.get('occurrence_start'),
                    'end': raw.get('occurrence_end'),
                    'value': raw.get('value'),
                    'observed_at': raw.get('available_at'),
                })
            warnings.append('휴대폰 생활 정보는 현재 저장된 이벤트 버전을 PC가 실제 수신한 시각부터 사용 가능한 것으로 취급합니다. 수동 수면 시각은 자가 보고이며, 센서 수면은 단계 검증을 통과한 것만 사용합니다.')
        by_night = {}
        for raw, observed, priority in candidates:
            try:
                known = known_onset_records([raw])
                if not known:
                    raise ValueError('sleep onset unavailable')
                if raw.get('sleep_start') not in (None, ''):
                    onset = instant(raw['sleep_start'])
                else:
                    onset = instant(raw.get('bed')) + timedelta(minutes=float(raw['latency_min']))
                end, seen = instant(raw.get('wake')), instant(observed)
                cutoff = cutoff_for_onset(onset)
                if not 30 <= (end-onset).total_seconds()/60 <= 960 or end > cutoff+timedelta(days=1) or seen < end:
                    raise ValueError('invalid sleep')
                row = {'participant_id': participant, 'cutoff': cutoff.isoformat(),
                       'sleep_start': onset.isoformat(), 'sleep_end': end.isoformat(), 'observed_at': seen.isoformat()}
                rank = (priority, (end-onset).total_seconds(), seen)
                if cutoff not in by_night or rank > by_night[cutoff][0]:
                    by_night[cutoff] = rank, row
            except (ValueError, TypeError, OverflowError):
                skipped += 1
        records = [v[1] for _, v in sorted(by_night.items())][-365:]
        valid_events = []
        for event in events:
            try:
                valid_events.extend(normalize_context([event], {participant}))
            except (ValueError, TypeError, OverflowError):
                skipped += 1
        if records:
            start = instant(records[0]['cutoff']).replace(hour=0)
            end = instant(records[-1]['cutoff']) + timedelta(hours=36)
            valid_events = [e for e in valid_events if start <= instant(e['start']) <= end]
        if len(valid_events) > 10000:
            original_count = len(valid_events)
            valid_events = _compact_steps(valid_events)
            warnings.append(f'걸음 구간을 동일 시간대·20시 이전 관측 여부별로 합산했습니다: 전체 {original_count}건 → {len(valid_events)}건. 20시 집계 걸음 합계는 유지합니다.')
        if len(valid_events) > 10000:
            raise ValueError('생활 정보가 분석 한도 10,000건을 넘습니다. 기간을 줄인 JSON으로 분석해주세요. 기록은 임의로 버리지 않았습니다.')
        valid_events = normalize_context(valid_events, {participant})
        if skipped:
            warnings.append(f'시각·관측 시점이 없거나 입력 계약을 충족하지 못한 {skipped}건은 제외했습니다.')
        if len(by_night) > 365:
            warnings.append('분석실 한도에 맞춰 최근 365개 야간을 불러왔습니다.')
        if not records:
            warnings.append('분석 가능한 수면 기록이 없습니다. 완료된 수면 기록을 동기화한 뒤 다시 불러오세요.')
        coverage = Counter(e['kind'] for e in valid_events)
        return {
            'payload': {
                'schema_version': 1,
                'mode': 'real',
                'cohorts': ['cmi', 'mesa'],
                'records': records,
                'context': valid_events,
            },
            'context_provenance': context_provenance,
            'summary': {
                'records': len(records),
                'context_events': len(valid_events),
                'coverage': {
                    'sleep': len(records),
                    **{k: coverage[k] for k in ('steps', 'exercise', 'caffeine', 'schedule', 'nap')},
                },
            },
            'warnings': warnings,
        }


def _pc_context(context, participant):
    events = []
    observed = context.get('observed_at')
    for plural, kind in (('schedules', 'schedule'), ('naps', 'nap'), ('caffeine', 'caffeine')):
        for item in context.get(plural, []):
            events.append({'participant_id': participant, 'kind': kind,
                           'start': item.get('at') if kind == 'caffeine' else item.get('start'),
                           'end': item.get('at') if kind == 'caffeine' else item.get('end'),
                           'value': item.get('mg') if kind == 'caffeine' else None,
                           'observed_at': observed})
    return events


def _compact_steps(events):
    """Preserve fixed-20:00 sums and availability; never split measured counts."""
    result, groups = [], {}
    for event in events:
        start, end, observed = instant(event['start']), instant(event['end']), instant(event['observed_at'])
        hour = start.replace(minute=0, second=0, microsecond=0)
        cutoff = start.replace(hour=20, minute=0, second=0, microsecond=0)
        if event['kind'] != 'steps' or end >= hour + timedelta(hours=1):
            result.append(event)
            continue
        key = event['participant_id'], hour, observed < cutoff
        if key not in groups:
            groups[key] = dict(event)
        else:
            merged = groups[key]
            merged['start'] = min(instant(merged['start']), start).isoformat()
            merged['end'] = max(instant(merged['end']), end).isoformat()
            merged['observed_at'] = max(instant(merged['observed_at']), observed).isoformat()
            merged['value'] += event['value']
    return result + list(groups.values())
