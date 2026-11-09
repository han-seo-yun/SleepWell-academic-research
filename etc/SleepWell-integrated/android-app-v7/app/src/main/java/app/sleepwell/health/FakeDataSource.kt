package app.sleepwell.health

import java.util.Calendar
import java.util.Random
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * 가상 데이터 생성기.
 *
 * 목적은 "화면과 export 를 실제 기기 없이 끝까지 테스트하는 것"이다. 그래서 값이
 * 그럴듯할 뿐 아니라 **실제로 겪게 될 골치아픈 상황을 일부러 포함**한다.
 *   - 수면 단계가 90분 주기로 돌고, 전반에 N3 가 많고 후반에 REM 이 길어진다
 *   - 자다가 잠깐 깨는 구간(WAKE)이 섞인다
 *   - 3일마다 한 번, 새벽 02:00~02:39 에 심박이 40분간 끊긴다 (자다가 시계가 벗겨진 상황)
 *   - 4일마다 한 번, 오후 14:10 에 20~40분 낮잠이 있다 → **같은 밤에 세션이 2개**
 *   - 일부 표본은 device 가 PHONE 이다 → watchOnly 필터를 실제로 검증할 수 있다
 *
 * 낮잠을 넣은 이유를 따로 적어둔다. 하루에 세션이 하나뿐이면 `(night, epoch_idx)` 가
 * 겹칠 일이 없어서, EpochAggregator 의 그 검사가 **한 번도 실행되지 않는다.**
 * 8월 평가코드가 night 버그를 놓친 원인이 정확히 이것이었다 — 검증용 더미에 문제
 * 상황이 없어서 문제 경로가 테스트되지 않았다. 같은 실수를 반복하지 않으려고
 * 일부러 어려운 입력을 만들어 둔다.
 *
 * seed 를 고정했으므로 같은 입력에 항상 같은 결과가 나온다. 테스트 결과를 비교할 수 있어야
 * 하기 때문이다.
 */
class FakeDataSource(
    private val seed: Long = 20260918L
) : HealthDataSource {

    override val name = "Fake"
    override val sourceTag = "fake"

    private val watch = DeviceInfo("Galaxy Watch5 (가상)", DeviceType.WATCH, "FAKE-WATCH-5")
    private val phone = DeviceInfo("Galaxy S23 (가상)", DeviceType.PHONE, "FAKE-PHONE")

    override suspend fun isAvailable() = true
    override suspend fun hasPermissions() = true
    override suspend fun requestPermissions(): HealthResult<Unit> = HealthResult.Success(Unit)

    // ── 수면 ────────────────────────────────────────────────────
    override suspend fun readSleepSessions(q: HealthQuery): HealthResult<List<SleepSession>> {
        val sessions = buildSessions(q)
        return HealthResult.Success(sessions)
    }

    /**
     * 조회 구간과 겹치는 밤들을 만든다.
     * 하루 전날 밤부터 살펴야 "새벽에 걸친 세션"을 놓치지 않는다.
     */
    private fun buildSessions(q: HealthQuery): List<SleepSession> {
        val out = mutableListOf<SleepSession>()
        var day = midnight(q.startMs) - DAY_MS      // 하루 전날부터
        while (day < q.endMs) {
            val rnd = Random(seed + day / DAY_MS)   // 날짜별 고정 시드

            // 취침: 23:10 ~ 00:10 사이
            val bedtime = day + 23 * HOUR_MS + (rnd.nextInt(61) - 0) * MIN_MS
            // 총 침대 시간: 6시간 20분 ~ 8시간 20분
            val inBed = (6 * 60 + 20 + rnd.nextInt(121)) * MIN_MS
            val wakeTime = bedtime + inBed

            if (wakeTime > q.startMs && bedtime < q.endMs) {
                out += SleepSession(
                    sessionId = "FAKE-${TimeFmt.day(bedtime)}",
                    startMs = bedtime,
                    endMs = wakeTime,
                    stages = buildStages(bedtime, wakeTime, rnd),
                    device = watch
                )
            }

            // 4일마다 낮잠. 14:10 은 그날 정오 이후이므로 같은 날 밤잠과 **같은 night** 에
            // 들어간다 (night = 정오~다음날 정오). epoch_idx 는 정오 기준이라
            // 낮잠 260번대 / 밤잠 1380번대로 번호가 겹치지 않는다.
            if ((day / DAY_MS) % 4L == 1L) {
                val napStart = day + 14 * HOUR_MS + 10 * MIN_MS
                val napEnd = napStart + (20 + rnd.nextInt(21)) * MIN_MS
                if (napEnd > q.startMs && napStart < q.endMs) {
                    out += SleepSession(
                        sessionId = "FAKE-NAP-${TimeFmt.day(napStart)}",
                        startMs = napStart,
                        endMs = napEnd,
                        stages = buildNapStages(napStart, napEnd, rnd),
                        device = watch
                    )
                }
            }
            day += DAY_MS
        }
        return out.sortedBy { it.startMs }
    }

    /**
     * 낮잠 구조. 밤잠과 달리 90분 주기를 다 돌지 못하고 LIGHT 에서 끝난다.
     * 20~40분 낮잠에 DEEP·REM 이 나오면 오히려 부자연스러워서 넣지 않았다.
     */
    private fun buildNapStages(startMs: Long, endMs: Long, rnd: Random): List<SleepStageSegment> {
        val segs = mutableListOf<SleepStageSegment>()
        var t = startMs

        fun push(stage: SleepStage, minutes: Int) {
            if (minutes <= 0 || t >= endMs) return
            val end = minOf(t + minutes * MIN_MS, endMs)
            segs += SleepStageSegment(t, end, stage)
            t = end
        }

        push(SleepStage.WAKE, 3 + rnd.nextInt(5))   // 눕고 잠들기까지
        push(SleepStage.LIGHT, 60)                  // 남은 시간 전부 (push 가 endMs 에서 자른다)
        return segs
    }

    /**
     * 수면 구조를 만든다. 실제 수면다원검사에서 보이는 흐름을 단순화한 것:
     *   입면 잠복기(WAKE) → [LIGHT → DEEP → LIGHT → REM] 을 약 90분 주기로 반복
     * 밤이 깊어질수록 DEEP 은 줄고 REM 은 길어진다.
     */
    private fun buildStages(startMs: Long, endMs: Long, rnd: Random): List<SleepStageSegment> {
        val segs = mutableListOf<SleepStageSegment>()
        var t = startMs

        fun push(stage: SleepStage, minutes: Int) {
            if (minutes <= 0 || t >= endMs) return
            val end = minOf(t + minutes * MIN_MS, endMs)
            // 같은 단계가 연달아 나오면 하나로 합친다 (구간 수를 불필요하게 늘리지 않기 위해)
            val last = segs.lastOrNull()
            if (last != null && last.stage == stage && last.endMs == t) {
                segs[segs.lastIndex] = last.copy(endMs = end)
            } else {
                segs += SleepStageSegment(t, end, stage)
            }
            t = end
        }

        // 입면 잠복기 — 불 끄고 잠들기까지
        push(SleepStage.WAKE, 5 + rnd.nextInt(16))

        var cycle = 0
        while (t < endMs) {
            // 밤이 진행되면서 N3 감소 / REM 증가
            // 계수는 실제 성인 수면 구성비에 맞춰 잡았다.
            // (교과서 값: N1 약 5%, N2 45~55%, N3 13~23%, REM 20~25%)
            // 처음 쓴 rem = 8 + cycle*6 은 REM 이 29.6% 까지 올라가 생리학적 범위를 벗어났다.
            // 가짜 데이터라도 비율이 틀리면 팀원이 이 값을 정상 기준으로 착각할 수 있어서 낮췄다.
            val n3 = max(0, 22 - cycle * 7 + rnd.nextInt(7))
            val rem = 5 + cycle * 4 + rnd.nextInt(6)

            // (2026-09-25) 워치 4단계로 변경: 예전 N1+N2 → LIGHT, N3 → DEEP
            push(SleepStage.LIGHT, 22 + rnd.nextInt(14))
            push(SleepStage.DEEP, n3)
            push(SleepStage.LIGHT, 8 + rnd.nextInt(10))
            push(SleepStage.REM, rem)

            // 주기 사이에 잠깐 깨는 일이 흔하다 (각성 지수)
            if (rnd.nextInt(100) < 55) push(SleepStage.WAKE, 1 + rnd.nextInt(5))
            cycle++
        }
        return segs
    }

    // ── 심박 ────────────────────────────────────────────────────
    override suspend fun readHeartRate(q: HealthQuery): HealthResult<List<HeartRateSample>> {
        val sessions = buildSessions(q)
        val segs = sessions.flatMap { it.stages }.sortedBy { it.startMs }
        val out = mutableListOf<HeartRateSample>()

        var t = q.startMs
        while (t < q.endMs) {
            val rnd = Random(seed * 31 + t / MIN_MS)

            // 3일마다 40분간 결측 — 자다가 시계가 벗겨진 상황.
            //
            // 시각을 새벽 02:00~02:39 로 둔 이유: 이 결측은 "결측 처리가 제대로 되는지"를
            // 검증하려고 일부러 넣은 것인데, 오후 시간대에 두면 수면 구간 밖이라
            // 수면 그래프에도 epoch CSV 에도 나타나지 않아 아무것도 검증하지 못한다.
            // 밤 안에 둬야 그래프에서 선 끊김으로, CSV 에서 빈 hr_mean 으로 확인된다.
            val dayIdx = (t - midnight(q.startMs)) / DAY_MS
            val minOfDay = (t - midnight(t)) / MIN_MS
            val inGap = (dayIdx % 3L == 2L) && minOfDay in 120..159
            if (inGap) { t += MIN_MS; continue }

            val stage = stageAt(t, segs)
            val base = when (stage) {
                SleepStage.DEEP -> 50f
                SleepStage.LIGHT -> 55f
                SleepStage.REM -> 62f
                else -> {
                    // 깨어 있을 때는 일주기 리듬 + 활동에 따른 변동
                    val h = minOfDay / 60.0
                    val diurnal = 8.0 * cos(2 * PI * (h - 16.0) / 24.0)   // 오후에 높고 새벽에 낮음
                    (70.0 - diurnal).toFloat()
                }
            }
            // REM 은 변동이 크고 N3 는 안정적이다
            val jitter = when (stage) {
                SleepStage.REM -> 6f
                SleepStage.DEEP -> 2f
                SleepStage.WAKE, SleepStage.UNKNOWN -> 9f
                else -> 3f
            }
            val bpm = base + (rnd.nextGaussian().toFloat() * jitter)

            // 20표본마다 한 번은 폰이 기록한 것으로 둔다 → watchOnly 필터 검증용
            val dev = if (rnd.nextInt(20) == 0) phone else watch
            out += HeartRateSample(t, bpm.coerceIn(38f, 165f), dev)
            t += MIN_MS
        }

        val filtered = if (q.watchOnly) out.filter { it.device?.deviceType == DeviceType.WATCH } else out
        return HealthResult.Success(filtered)
    }

    // ── 걸음 ────────────────────────────────────────────────────
    override suspend fun readSteps(q: HealthQuery): HealthResult<List<StepSample>> {
        val sessions = buildSessions(q)
        val out = mutableListOf<StepSample>()
        val bucket = 10 * MIN_MS

        var t = (q.startMs / bucket) * bucket
        while (t < q.endMs) {
            val rnd = Random(seed * 17 + t / bucket)
            val asleep = sessions.any { t >= it.startMs && t < it.endMs }

            val count = if (asleep) {
                // 자면서도 뒤척임으로 몇 걸음이 잡히는 일이 있다
                if (rnd.nextInt(100) < 8) rnd.nextInt(12) else 0
            } else {
                val h = (t - midnight(t)) / HOUR_MS.toDouble()
                // 출근/등교 시간대와 저녁에 봉우리가 있는 형태
                val peak = 1.0 - abs(h - 9.0) / 6.0
                val peak2 = 1.0 - abs(h - 19.0) / 5.0
                val shape = max(0.12, max(peak, peak2))
                (shape * (180 + rnd.nextInt(320))).roundToInt()
            }
            out += StepSample(t, t + bucket, count, watch)
            t += bucket
        }
        return HealthResult.Success(out)
    }

    // ── 유틸 ────────────────────────────────────────────────────
    private fun stageAt(ms: Long, segs: List<SleepStageSegment>): SleepStage =
        segs.firstOrNull { ms >= it.startMs && ms < it.endMs }?.stage ?: SleepStage.WAKE

    /** 해당 시각이 속한 날의 로컬 자정 */
    private fun midnight(ms: Long): Long = Calendar.getInstance(APP_ZONE).apply {   // (2026-09-25) 앱 기준 시간대
        timeInMillis = ms
        set(Calendar.HOUR_OF_DAY, 0)
        set(Calendar.MINUTE, 0)
        set(Calendar.SECOND, 0)
        set(Calendar.MILLISECOND, 0)
    }.timeInMillis

    private companion object {
        const val MIN_MS = 60_000L
        const val HOUR_MS = 60 * MIN_MS
        const val DAY_MS = 24 * HOUR_MS
    }
}
