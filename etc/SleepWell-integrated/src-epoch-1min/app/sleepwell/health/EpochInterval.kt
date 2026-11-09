package app.sleepwell.health

/**
 * 모델 입력 epoch 간격 (두 가지 값을 모두 지원).
 *
 * 왜 enum인가:
 *   - 두 값을 코드 한 곳에 정의해두면, 나중에 "분리 10분 epoch" 같은 게 필요해질 때
 *     여기 한 줄 추가로 끝나고, 하드코딩된 30_000L 을 다시 찾을 필요가 없다.
 *   - 하드코딩(`30_000L`)이 파이프라인 두 군데에 흩어져 있었다. 이번에 `EpochInterval`
 *     로 모아서, 1분으로 바꿀 때 빠뜨리는 일이 없게 한다.
 *
 * ★ 2026-10-02 정정 — 앱 export 격자도 1분으로 바뀌었다
 *
 *   아래 주석은 초판(2026-09-27) 기준이고, 더 이상 사실이 아니다. 그대로 두면
 *   "export 는 30초 유지"라고 잘못 안내하므로 고쳤다.
 *
 *   2026-09-26 회의 결정. 근거는 "삼성 데이터의 기본 시간 단위가 1분이므로" 다.
 *   결정 문구: "현재 30초 epoch → 1분 epoch. 데이터셋과 앱을 모두 1분으로 통일".
 *   소빈이 `HealthModels.EPOCH_MS` 를 `60_000L` 로 바꾸고 export 파일을 `epoch_1m.csv` 로
 *   바꿨다. 즉 **모델 입력과 export 가 모두 1분**이고, 이 enum 과 `EPOCH_MS` 가 같은 값이다.
 *
 *   주의 — 초판(2026-09-27)에 내가 "export 는 30초 유지가 안전하다"고 적은 건 잘못이다.
 *   그때 30초를 유지할 근거로 세운 것은 "Sleep-EDF 가 30초 표준이라"였는데,
 *   그건 **근거가 아니라 파생 제약**을 뒤집은 것이다.
 *   Sleep-EDF 가 30초라서 생기는 결과는 "1분 예측을 30초 정답과 비교하려면 역집계 규칙이
 *   먼저 필요하다"는 제약이지, 30초를 남겨야 할 이유가 아니다.
 *   결정 근거는 삼성 데이터의 기본 단위다.
 *
 *   그래서 아래 "SEC30" 은 예전 기준선을 남겨 둔 선택지일 뿐, 현재 앱 기본은 아니다.
 *   되돌리려면 두 곳(`EpochInterval.DEFAULT` 와 `HealthModels.EPOCH_MS`)을 같이 바꿔야 한다.
 *
 * ★ 남은 위험 — Sleep-EDF 평가 join
 *
 *   `EpochAggregator.epochIdx` 가 "그 밤 정오부터 몇 번째 1분 칸"으로 바뀌었다.
 *   그런데 Sleep-EDF 정답(`onset_sec`)은 30초 기준이라 정답 쪽 epoch_idx 와 단위가 다르다.
 *   평가코드는 `(subject_id, night, epoch_idx)` 로 inner join 하므로,
 *   단위만 다르면 **에러 없이 조용히 틀어진다** (주석 기록: night 어긋나면 F1 −0.210 / Kappa −0.222).
 *   1분 예측 → 30초 정답 비교 규칙은 소빈 쪽(평가코드)에서 정한다.
 *   그 규칙이 정해지기 전에는 1분 성능 수치를 보고서에 쓰면 안 된다.
 */
enum class EpochInterval(
    /** epoch 길이 (밀리초) */
    val intervalMs: Long,
    /** epoch 길이 (초) */
    val seconds: Int,
    /** 화면·리포트 표시용 라벨 */
    val label: String
) {
    /** 2026-09-27 초판 기준선. 지금은 비교·회 실험용으로만 남는다. */
    SEC30(30_000L, 30, "30초"),

    /** 팀 공통 기준 (2026-09-26 회의). 앱 export(EPOCH_MS)와 같은 값. */
    MIN1(60_000L, 60, "1분");

    companion object {
        /**
         * 파이프라인 기본값. 호출 측에서 명시하지 않으면 이 값이 쓰인다.
         *
         * 2026-10-02: SEC30 → MIN1 로 바꾼다.
         *   팀 기준이 1분인데 기본값만 30초면, 인자를 빠뜨린 호출이 조용히 30초로 계산된다.
         *   그 결과는 화면·export(1분)와 어긋나는데 아무도 모른다. 이게 바로 막는 종류의 실수다.
         *   30초가 필요하면 SEC30 을 명시한다. (30초/1분 비교 실험에서 쓴다)
         */
        val DEFAULT: EpochInterval = MIN1

        /** epoch 길이(초)로 조회. 없으면 null. */
        fun fromSeconds(seconds: Int): EpochInterval? =
            entries.firstOrNull { it.seconds == seconds }
    }
}

/**
 * epoch 경계 정렬.
 *
 * 왜 필요한가:
 *   타임라인 시작 시각이 epoch 경계에 떨어지지 않으면 모든 epoch가 ":30" 처럼 어긋난
 *   시각에 놓인다. TwoProcessModel의 C_sleep는 epoch 시각의 시계 시각(시+분/60+초/3600)을
 *   그대로 쓰므로, 어긋나면 C가 실제보다 최대 (interval/2) 만큼 밀린다.
 *   30초에서는 최대 15초, 1분에서는 최대 30초 차이 → 하루 리듬 위상 계산에 그대로 남는다.
 *
 * 기준선은 Unix epoch(0ms). 30초·60초 모두 0에서 나누어떨어지므로 KST 자정/정오와도 일치한다.
 *   KST는 UTC+9:00으로 정수 시간대라, 60_000의 배수인 시각의 초·분은 항상 0이다.
 */
object EpochGrid {

    /** 시각을 epoch 경계로 내림(floor). */
    fun floorTo(tsMs: Long, interval: EpochInterval): Long {
        val m = interval.intervalMs
        return tsMs - Math.floorMod(tsMs, m)
    }

    /** 시각을 epoch 경계로 올림(ceil). 이미 경계면 그대로. */
    fun ceilTo(tsMs: Long, interval: EpochInterval): Long {
        val m = interval.intervalMs
        val rem = Math.floorMod(tsMs, m)
        return if (rem == 0L) tsMs else tsMs + (m - rem)
    }

    /** 시각이 epoch 경계에 떨어지는지. */
    fun isAligned(tsMs: Long, interval: EpochInterval): Boolean =
        Math.floorMod(tsMs, interval.intervalMs) == 0L
}