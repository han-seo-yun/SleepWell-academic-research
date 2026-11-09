package test

import app.sleepwell.health.EpochInterval
import app.sleepwell.health.SleepEpochConverter
import app.sleepwell.health.SleepSession
import app.sleepwell.health.SleepStage
import app.sleepwell.health.SleepStageSegment

/**
 * ★ 회귀 테스트 — SleepEpochConverter (epoch 격자 만들기)
 *
 * 이 함수는 2026-09-27 에 처음 만들었고, 규칙이 두 가지로 복잡해서 테스트가 꼭 필요하다.
 *
 *   1. **전체 타임라인**이어야 한다. 수면 구간만 넣으면 세션 사이를 전부 수면으로
 *      간주해서 S 계산이 완전히 틀어진다. (next-steps-채윤-다음단계.md 소빈 지적)
 *   2. **가장 긴 겹침 우선**으로 isSleep 을 정한다. 한 epoch 에 단계가 여러 개 걸치면
 *      가장 오래 겹치는 걸 쓴다.
 *
 * 그리고 epoch 간격 인자(MIN1/SEC30)를 새로 넣었으니, **격자 폭이 진짜 바뀌는지**도 봐야 한다.
 */
object SleepEpochConverterTest {

    private fun seg(start: String, end: String, stage: SleepStage) =
        SleepStageSegment(Kst.ms(start), Kst.ms(end), stage)

    private fun session(id: String, vararg segs: SleepStageSegment) = SleepSession(
        sessionId = id,
        startMs = segs.minOf { it.startMs },
        endMs = segs.maxOf { it.endMs },
        stages = segs.toList()
    )

    fun run() {
        gridWidthFollowsInterval()
        includesGapBetweenSessions()
        wakeStageIsNotSleep()
        longestOverlapWins()
        lastPartialEpochIncluded()
        emptyAndDegenerateInputs()
        alignment()
    }

    /** epoch 격자가 요청한 간격 그대로인지. 1분 전환의 핵심. */
    private fun gridWidthFollowsInterval() {
        T.group("SleepEpochConverter — 격자 폭이 간격을 따른다")

        val sessions = listOf(
            session("S1",
                seg("2026-09-14 02:00:00", "2026-09-14 04:00:00", SleepStage.LIGHT))
        )
        val start = Kst.ms("2026-09-14 00:00:00")
        val end = Kst.ms("2026-09-14 06:00:00")

        for (iv in EpochInterval.entries) {
            val out = SleepEpochConverter.toEpochTimeline(sessions, start, end, iv, false)
            val steps = out.zipWithNext { a, b -> b.timestampMs - a.timestampMs }.distinct()
            T.assertEquals("${iv.label} : 모든 간격이 ${iv.intervalMs}ms 로 동일", 1, steps.size,
                why = "간격이 섞이면 dt 계산이 꼬인다. 두 번째 간격 값 = ${steps.getOrNull(1)}")
            T.assertEquals("${iv.label} : 첫 epoch 가 시작 시각", start, out.first().timestampMs)
        }

        val n30 = SleepEpochConverter.toEpochTimeline(sessions, start, end, EpochInterval.SEC30, false).size
        val n60 = SleepEpochConverter.toEpochTimeline(sessions, start, end, EpochInterval.MIN1, false).size
        T.assertEquals("30초 칸 수 (6시간)", 720, n30)
        T.assertEquals("1분 칸 수 (6시간)", 360, n60)
    }

    /** [핵심] 세션 사이 깨어 있던 시간도 WAKE 로 포함돼야 한다. 빠지면 S 가 완전히 틀어진다. */
    private fun includesGapBetweenSessions() {
        T.group("SleepEpochConverter — 세션 사이 WAKE 포함")

        val sessions = listOf(
            session("S1", seg("2026-09-14 02:00:00", "2026-09-14 04:00:00", SleepStage.LIGHT)),
            session("S2", seg("2026-09-14 14:00:00", "2026-09-14 16:00:00", SleepStage.DEEP))
        )
        val start = Kst.ms("2026-09-14 00:00:00")
        val end = Kst.ms("2026-09-14 18:00:00")
        val out = SleepEpochConverter.toEpochTimeline(sessions, start, end, EpochInterval.MIN1, false)

        val asleep = out.filter { it.isSleep }
        T.assertEquals("전체 칸 수 (18시간/1분)", 1080, out.size)
        T.assertEquals("수면으로 잡힌 칸 수 (2시간 × 2)", 240, asleep.size)

        val gap = out.filter { it.timestampMs in Kst.ms("2026-09-14 10:00:00") until Kst.ms("2026-09-14 12:00:00") }
        T.assertTrue("세션 사이 2시간 전부 WAKE (칸 수=${gap.size})", gap.isNotEmpty() && gap.none { it.isSleep },
            "세션 사이를 수면으로 잡았다 = next-steps 문서가 경고한 그대로의 버그.")

        val inS2 = out.filter { it.timestampMs in Kst.ms("2026-09-14 14:30:00") until Kst.ms("2026-09-14 14:31:00") }
        T.assertTrue("두 번째 세션 안은 수면", inS2.isNotEmpty() && inS2.all { it.isSleep })
    }

    /** WAKE / UNKNOWN 은 수면이 아니다. */
    private fun wakeStageIsNotSleep() {
        T.group("SleepEpochConverter — WAKE/UNKNOWN 은 수면 아님")

        val sessions = listOf(
            session("S1",
                seg("2026-09-14 02:00:00", "2026-09-14 03:00:00", SleepStage.WAKE),
                seg("2026-09-14 03:00:00", "2026-09-14 04:00:00", SleepStage.UNKNOWN),
                seg("2026-09-14 04:00:00", "2026-09-14 06:00:00", SleepStage.LIGHT))
        )
        val out = SleepEpochConverter.toEpochTimeline(
            sessions, Kst.ms("2026-09-14 02:00:00"), Kst.ms("2026-09-14 06:00:00"),
            EpochInterval.MIN1, false)

        fun flagAt(t: String) = out.single { it.timestampMs == Kst.ms(t) }.isSleep
        T.assertFalse("WAKE 구간은 false", flagAt("2026-09-14 02:30:00"))
        T.assertFalse("UNKNOWN 구간은 false", flagAt("2026-09-14 03:30:00"))
        T.assertTrue("LIGHT 구간은 true", flagAt("2026-09-14 05:00:00"))
    }

    /** 한 epoch 에 두 단계가 걸치면 **더 길게 겹치는 쪽**을 쓴다. */
    private fun longestOverlapWins() {
        T.group("SleepEpochConverter — 가장 긴 겹침 우선")

        // 1분 epoch 하나에 LIGHT 40초 + WAKE 20초
        val sessions = listOf(
            session("S1",
                seg("2026-09-14 02:00:00", "2026-09-14 02:00:40", SleepStage.LIGHT),
                seg("2026-09-14 02:00:40", "2026-09-14 02:01:00", SleepStage.WAKE))
        )
        val out = SleepEpochConverter.toEpochTimeline(
            sessions, Kst.ms("2026-09-14 02:00:00"), Kst.ms("2026-09-14 02:01:00"),
            EpochInterval.MIN1, false)
        T.assertEquals("칸 수", 1, out.size)
        T.assertTrue("LIGHT 40초 > WAKE 20초 이므로 수면", out[0].isSleep)

        // 반대: LIGHT 20초 + WAKE 40초
        val sessions2 = listOf(
            session("S2",
                seg("2026-09-14 02:00:00", "2026-09-14 02:00:20", SleepStage.LIGHT),
                seg("2026-09-14 02:00:20", "2026-09-14 02:01:00", SleepStage.WAKE))
        )
        val out2 = SleepEpochConverter.toEpochTimeline(
            sessions2, Kst.ms("2026-09-14 02:00:00"), Kst.ms("2026-09-14 02:01:00"),
            EpochInterval.MIN1, false)
        T.assertFalse("WAKE 40초 > LIGHT 20초 이므로 각성", out2[0].isSleep)
    }

    /** 마지막 조각이 interval 보다 짧아도 포함한다 (합의안 확정 항목). */
    private fun lastPartialEpochIncluded() {
        T.group("SleepEpochConverter — 마지막 부분 칸 포함")

        val sessions = listOf(
            session("S1", seg("2026-09-14 02:00:00", "2026-09-14 02:00:30", SleepStage.LIGHT))
        )
        val out = SleepEpochConverter.toEpochTimeline(
            sessions, Kst.ms("2026-09-14 02:00:00"), Kst.ms("2026-09-14 02:00:30"),
            EpochInterval.MIN1, false)
        T.assertEquals("30초짜리 구간도 1분 칸 1개로 잡힘", 1, out.size)
        T.assertTrue("부분 칸도 수면으로 판정", out[0].isSleep)
    }

    /** 빈 입력 / 역전 구간. 예외를 던지지 않고 빈 리스트를 낸다. */
    private fun emptyAndDegenerateInputs() {
        T.group("SleepEpochConverter — 빈/역전 입력")

        T.assertEquals("세션 없으면 빈 리스트", 0,
            SleepEpochConverter.toEpochTimeline(emptyList(), 0L, 100_000L, EpochInterval.MIN1, false).size)
        T.assertEquals("end <= start 면 빈 리스트", 0,
            SleepEpochConverter.toEpochTimeline(
                listOf(session("S", seg("2026-09-14 02:00:00", "2026-09-14 03:00:00", SleepStage.LIGHT))),
                100_000L, 100_000L, EpochInterval.MIN1, false).size)
        T.assertEquals("end < start 면 빈 리스트", 0,
            SleepEpochConverter.toEpochTimeline(
                listOf(session("S", seg("2026-09-14 02:00:00", "2026-09-14 03:00:00", SleepStage.LIGHT))),
                200_000L, 100_000L, EpochInterval.MIN1, false).size)
    }

    /** alignToEpochBoundary=true 면 시작이 경계에 맞아야 한다. */
    private fun alignment() {
        T.group("SleepEpochConverter — 경계 정렬")

        val sessions = listOf(
            session("S1", seg("2026-09-14 02:00:00", "2026-09-14 04:00:00", SleepStage.LIGHT))
        )
        val skewed = Kst.ms("2026-09-14 00:00:37")   // 경계가 아님

        val aligned = SleepEpochConverter.toEpochTimeline(sessions, skewed, Kst.ms("2026-09-14 06:00:00"), EpochInterval.MIN1, true)
        T.assertTrue("정렬 시 첫 epoch 가 1분 경계", EpochGridFlag.isAligned(aligned.first().timestampMs, EpochInterval.MIN1),
            "정렬했는데 경계에 안 맞는다.")

        val raw = SleepEpochConverter.toEpochTimeline(sessions, skewed, Kst.ms("2026-09-14 06:00:00"), EpochInterval.MIN1, false)
        T.assertEquals("정렬 안 하면 시작 시각 그대로", skewed, raw.first().timestampMs)

        T.assertTrue("정렬하면 시작 시각이 앞으로 이동", aligned.first().timestampMs <= skewed)
    }
}

/** 테스트 안에서 쓸 짧은 별칭 (EpochGrid 는 같은 파일에서 쓰면 되므로 여기선 생략) */
private object EpochGridFlag {
    fun isAligned(ms: Long, iv: EpochInterval): Boolean =
        Math.floorMod(ms, iv.intervalMs) == 0L
}