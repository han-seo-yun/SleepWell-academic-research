package app.sleepwell.health

import java.util.Calendar
import kotlin.math.roundToInt

/**
 * 센서 시계열 → 30초 epoch 격자로 묶기.
 *
 * 왜 필요한가: 심박은 몇십 초~몇 분 간격으로 띄엄띄엄 오고, 걸음은 10분 구간 합계로 오고,
 * 수면 단계는 수십 분짜리 긴 구간으로 온다. 서로 시간 단위가 달라서 그대로는 한 표에 못 담는다.
 * 우리 팀의 모든 데이터셋(Sleep-EDF·Walch·CMI)이 30초 epoch 로 통일돼 있으므로 앱 export 도
 * 같은 격자에 맞춘다. 그래야 별도 변환 없이 학습·평가에 들어간다.
 *
 * ★ night / epoch_idx 규칙 (2026-09-18 Sleep-EDF 검증에서 얻은 교훈)
 *
 *   평가코드는 `(subject_id, night, epoch_idx)` 세 값으로 정답과 inner join 한다.
 *   따라서 이 두 값은 **행 번호가 아니라 시각 좌표**여야 하고, 같은 데이터를 언제
 *   어떤 범위로 조회해도 **같은 값이 나와야** 한다. 그렇지 않으면 정답과 시점이
 *   어긋나는데도 평가코드가 잡지 못해 **점수가 조용히 틀린다.**
 *   (실측: night 이 어긋나면 F1(macro) −0.210 / Kappa −0.222, 오류 메시지 0줄)
 *
 *   그래서:
 *   - `night` = 그 밤이 속한 **날짜 yyyyMMdd**. 정오~다음날 정오를 한 밤으로 본다.
 *     조회 순번(1, 2, 3…)을 쓰면 조회 범위를 바꿀 때마다 같은 밤의 번호가 달라진다.
 *   - `epoch_idx` = **그 밤 날짜의 정오부터 몇 번째 30초 칸인지.**
 *     `epoch_idx × 30 = 정오 기준 경과 초` — Sleep-EDF 정답의 `onset_sec` 과 같은 성질.
 *     세션 시작을 0으로 잡으면 같은 날 낮잠과 밤잠이 둘 다 0에서 시작해 충돌한다.
 *   - 단계를 모르는 구간(UNKNOWN)도 번호를 그대로 소비하고 stageLabel = -1 로 남긴다.
 *     값이 없다고 행을 건너뛰면서 번호를 당겨 붙이면 그 뒤 전체가 밀린다.
 *     Sleep-EDF GT 197개 중 80개 파일에서 실제로 이 위험이 확인됐다.
 */
data class EpochRow(
    val subjectId: String,
    /** 그 밤이 속한 날짜 = yyyyMMdd 정수 (정오~다음날 정오를 한 밤으로 묶음) */
    val night: Int,
    /** 세션 식별자. 같은 night 안에 낮잠+밤잠이 함께 올 수 있어 구분용으로 함께 내보낸다 */
    val sessionId: String,
    /** night 날짜의 정오부터 몇 번째 30초 칸인지. `epochIdx × 30 = 정오 기준 경과 초` */
    val epochIdx: Int,
    /** 0=W 1=N1 2=N2 3=N3 4=REM, 모르면 -1 (평가에서 자동 제외됨) */
    val stageLabel: Int,
    val timeMs: Long,
    /** 해당 30초 구간의 심박 평균. 표본이 없으면 null → CSV 에서 빈칸 */
    val hrMean: Float?,
    /** 해당 30초 구간에 배분된 걸음 수 */
    val steps: Int
)

object EpochAggregator {

    /**
     * 수면 세션별로 30초 격자를 만들고 심박·걸음을 그 격자에 배분한다.
     * 낮 시간 데이터는 여기에 들어가지 않는다 (hr.csv / steps.csv 로 따로 나감).
     */
    fun aggregate(snapshot: HealthSnapshot): List<EpochRow> {
        val hr = snapshot.heartRate.sortedBy { it.timeMs }
        val steps = snapshot.steps.sortedBy { it.startMs }
        val out = mutableListOf<EpochRow>()

        snapshot.sleepSessions
            .sortedBy { it.startMs }
            .forEach { session ->
                if (session.endMs <= session.startMs) return@forEach   // 길이 0 이하 세션은 버린다

                // 이 세션이 속한 밤의 기준점 = 그 밤 날짜의 정오
                val anchor = nightAnchorMs(session.startMs)
                val night = nightIdOf(anchor)
                val segs = session.stages.sortedBy { it.startMs }

                // 격자를 세션 시작이 아니라 정오에 맞춘다. 그래서 첫 칸 번호가 0이 아니고,
                // 첫 칸은 취침 시각보다 최대 29초 앞에서 시작할 수 있다(30초 격자에 맞춰 내림).
                // 마지막 칸이 잘려도 번호는 유지한다.
                val firstIdx = Math.floorDiv(session.startMs - anchor, EPOCH_MS).toInt()
                val lastIdx = Math.floorDiv(session.endMs - 1 - anchor, EPOCH_MS).toInt()
                val gridStart = anchor + firstIdx * EPOCH_MS

                // 정렬된 목록을 앞에서부터 훑기 위한 포인터 (매 epoch 마다 전체를 다시 스캔하지 않음)
                var hrPtr = hr.indexOfFirst { it.timeMs >= gridStart }.let { if (it < 0) hr.size else it }
                var stepPtr = steps.indexOfFirst { it.endMs > gridStart }.let { if (it < 0) steps.size else it }

                for (i in firstIdx..lastIdx) {
                    val from = anchor + i * EPOCH_MS
                    val to = from + EPOCH_MS

                    // ── 단계: 이 30초 칸을 가장 많이 덮는 구간의 단계를 쓴다 ──
                    val stage = dominantStage(segs, from, to)

                    // ── 심박 평균 ──
                    var sum = 0f
                    var n = 0
                    var p = hrPtr
                    while (p < hr.size && hr[p].timeMs < to) {
                        if (hr[p].timeMs >= from) { sum += hr[p].bpm; n++ }
                        p++
                    }
                    while (hrPtr < hr.size && hr[hrPtr].timeMs < to) hrPtr++
                    val hrMean = if (n > 0) sum / n else null

                    // ── 걸음: 겹치는 시간 비율로 나눠 배분한다 ──
                    // 10분 버킷(=20 epoch) 값을 한 칸에 몰아주면 걸음이 한 순간에 몰린 것처럼
                    // 왜곡되므로, 겹친 길이 비례로 쪼갠다.
                    var stepSum = 0.0
                    var q = stepPtr
                    while (q < steps.size && steps[q].startMs < to) {
                        val s = steps[q]
                        val overlap = minOf(to, s.endMs) - maxOf(from, s.startMs)
                        val span = (s.endMs - s.startMs).coerceAtLeast(1L)
                        if (overlap > 0) stepSum += s.count.toDouble() * overlap / span
                        q++
                    }
                    while (stepPtr < steps.size && steps[stepPtr].endMs <= to) stepPtr++

                    out += EpochRow(
                        subjectId = snapshot.subjectId,
                        night = night,
                        sessionId = session.sessionId,
                        epochIdx = i,
                        stageLabel = stage.code,
                        timeMs = from,
                        hrMean = hrMean,
                        steps = stepSum.roundToInt()
                    )
                }
            }
        return out
    }

    /**
     * 그 밤의 기준 시각 = 밤이 속한 날짜의 **정오**(로컬 시간).
     *
     * 자정을 기준으로 하면 하룻밤이 두 날짜로 쪼개진다(23:40 취침 → 07:00 기상).
     * 그래서 수면 연구의 관례대로 **정오~다음날 정오**를 한 밤으로 묶는다.
     * 취침 시각에서 12시간을 뺀 날짜가 그 밤의 날짜다.
     *   23:40 9/17 → 11:40 9/17 → 9/17 밤
     *   00:30 9/18 → 12:30 9/17 → 9/17 밤   (자정을 넘겨 잠들어도 같은 밤)
     *   14:10 9/17 → 02:10 9/17 → 9/17 밤   (같은 날 낮잠도 같은 밤에 들어간다)
     *
     * 한국은 서머타임이 없어 정오 간격이 항상 24시간이다. 다른 표준시로 확장할 때는
     * Calendar 가 로컬 정오를 다시 계산해 주므로 이 함수는 그대로 쓸 수 있다.
     */
    private fun nightAnchorMs(sessionStartMs: Long): Long = Calendar.getInstance().apply {
        timeInMillis = sessionStartMs - 12 * 3_600_000L
        set(Calendar.HOUR_OF_DAY, 12)
        set(Calendar.MINUTE, 0)
        set(Calendar.SECOND, 0)
        set(Calendar.MILLISECOND, 0)
    }.timeInMillis

    /** 기준 정오의 날짜를 yyyyMMdd 정수로. 조회 범위가 바뀌어도 값이 변하지 않는다. */
    private fun nightIdOf(anchorMs: Long): Int = Calendar.getInstance().let { c ->
        c.timeInMillis = anchorMs
        c.get(Calendar.YEAR) * 10_000 + (c.get(Calendar.MONTH) + 1) * 100 + c.get(Calendar.DAY_OF_MONTH)
    }

    /** 30초 칸을 가장 오래 덮는 단계. 겹치는 구간이 없으면 UNKNOWN. */
    private fun dominantStage(
        segs: List<SleepStageSegment>,
        from: Long,
        to: Long
    ): SleepStage {
        var best = SleepStage.UNKNOWN
        var bestOverlap = 0L
        for (s in segs) {
            if (s.startMs >= to) break            // 정렬되어 있으므로 더 볼 필요 없음
            val overlap = minOf(to, s.endMs) - maxOf(from, s.startMs)
            if (overlap > bestOverlap) { bestOverlap = overlap; best = s.stage }
        }
        return best
    }

    /**
     * 자기 검증. export 직전에 돌려서 규격이 깨지지 않았는지 확인한다.
     * 문제 목록을 돌려주며, 비어 있으면 정상이다.
     *
     * "만들었으니 맞을 것"이라고 넘기지 않기 위한 장치다 — Sleep-EDF GT 도 같은 방식으로
     * 검증해서 오정렬 위험을 찾아냈다.
     */
    fun selfCheck(rows: List<EpochRow>): List<String> {
        val problems = mutableListOf<String>()
        if (rows.isEmpty()) return listOf("epoch 행이 0개입니다. 수면 세션이 조회되지 않았습니다.")

        // (1) (night, epoch_idx) 조합은 전체에서 유일해야 한다.
        //     평가코드가 (subject_id, night, epoch_idx) 로 정답과 inner join 하므로,
        //     겹치면 예측 1행이 정답 여러 행에 동시에 걸려 점수가 조용히 틀린다.
        //     실측된 피해: F1(macro) −0.210 / Kappa −0.222, 오류 메시지 0줄.
        val dupKeys = rows.groupingBy { it.night to it.epochIdx }.eachCount().filterValues { it > 1 }
        if (dupKeys.isNotEmpty()) {
            val (n, e) = dupKeys.keys.first()
            problems += "(night, epoch_idx) 중복 ${dupKeys.size}개 조합 — 같은 밤에 " +
                    "시간이 겹치는 세션이 있습니다. 예: night $n / epoch_idx $e"
        }

        // (2) 시각과 epoch_idx 가 30초 격자로 맞는지.
        //     epoch_idx 는 행 번호가 아니라 시각 좌표라서, 같은 night 안에서는
        //     timeMs − epochIdx × 30초 가 항상 그 밤의 정오여야 한다 = 상수.
        rows.groupBy { it.night }.forEach { (night, group) ->
            val anchors = group.map { it.timeMs - it.epochIdx * EPOCH_MS }.distinct()
            if (anchors.size != 1)
                problems += "night $night: 시각과 epoch_idx 가 어긋난 행이 있습니다 " +
                        "(기준시각이 ${anchors.size}종류, 30초 격자 위반)."
        }

        // (3) 한 세션 안에서는 번호를 건너뛰지 않아야 한다.
        //     모르는 칸도 stage_label = −1 로 번호를 소비한다. 건너뛰며 번호를 당겨
        //     붙이면 그 뒤 전체가 밀리는데 평가코드가 잡지 못한다.
        //     세션 **사이**의 빈 번호는 정상이다 — 낮잠과 밤잠 사이는 비어 있어야 한다.
        rows.groupBy { it.sessionId }.forEach { (sid, group) ->
            val idx = group.map { it.epochIdx }.sorted()
            if (idx.last() - idx.first() + 1 != idx.size)
                problems += "세션 $sid: epoch_idx 에 빠진 번호가 있습니다 (세션 안에서는 건너뜀 금지)."
        }

        val badLabel = rows.count { it.stageLabel !in -1..4 }
        if (badLabel > 0) problems += "stage_label 이 -1~4 밖인 행 ${badLabel}개."

        return problems
    }

    /** 화면 요약용 통계 */
    fun summarize(rows: List<EpochRow>): EpochSummary {
        val byStage = rows.groupingBy { it.stageLabel }.eachCount()
        val sleepEpochs = rows.count { it.stageLabel in 1..4 }
        return EpochSummary(
            totalEpochs = rows.size,
            nights = rows.map { it.night }.distinct().size,
            sessions = rows.map { it.sessionId }.distinct().size,
            sleepDurationMs = sleepEpochs * EPOCH_MS,
            stageCounts = byStage,
            unknownEpochs = byStage[-1] ?: 0,
            hrCoverage = if (rows.isEmpty()) 0f
                         else rows.count { it.hrMean != null }.toFloat() / rows.size
        )
    }
}

data class EpochSummary(
    val totalEpochs: Int,
    /** 서로 다른 night(yyyyMMdd) 개수 = 며칠 밤인지 */
    val nights: Int,
    /** 수면 세션 개수. 낮잠이 있으면 nights 보다 많다 */
    val sessions: Int,
    /** 실제 수면 시간 = W 를 제외한 epoch 합계 */
    val sleepDurationMs: Long,
    val stageCounts: Map<Int, Int>,
    val unknownEpochs: Int,
    /** 심박이 채워진 epoch 비율. 낮으면 시계를 벗었거나 조회가 덜 된 것. */
    val hrCoverage: Float
)
