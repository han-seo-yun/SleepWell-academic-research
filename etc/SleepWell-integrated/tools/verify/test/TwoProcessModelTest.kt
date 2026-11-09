package test

import app.sleepwell.health.TwoProcessModel
import java.util.Locale

/**
 * ★ 회귀 테스트 — Two-Process 모델 (어떤 epoch 버전에서도 컴파일된다)
 *
 * EpochInterval 을 참조하지 않으므로 src/ · src-epoch-30s/ · src-epoch-1min/ 세 폴더
 * 어디에 붙여도 컴파일된다. 그래서 30초 버전에도 그대로 돌린다.
 *
 * ---------------------------------------------------------------------------
 * 왜 이 테스트가 있는가 — 실제로 난 버그
 *
 *   clockHours() 가 아래처럼 적혀 있었다:
 *       return local.hour.toDouble()
 *           + local.minute.toDouble() / 60.0
 *           + local.second.toDouble() / 3600.0
 *
 *   Kotlin 은 `return` 뒤에서 줄이 바뀌고 + 로 시작하면 그 줄을 **새 문장(단항 +)** 으로 읽는다.
 *   그래서 return 은 hour 만 돌려주고 분·초는 조용히 버려졌다. 컴파일러도 조용히 통과시켰다.
 *
 *   증상: 한 시간 안에서 C 가 전혀 안 변한다 (매시간 계단으로만 뛴다).
 *   확정 전 실측: 12:24 -> 12.000000 / 12:24:30 -> 12.408333 등 5/5 불일치.
 *
 *   왜 못 잡았는가: 회귀 비교는 "30초 vs 1분" 만 봤고 두 버전이 같은 버그를 공유해서
 *   차이가 안 났다. 사람이 봐야 할 건 "시간이 흐르는데 값이 안 움직이는가" 인데 그런
 *   테스트가 없었다. 이 파일이 그 빈틈을 메운다.
 *
 *   private 함수 를 직접 못 불러서 공개 API(`compute`)로 **행동**을 본다.
 *   private 을 visibility 를 올려 테스트하는 것보다 behavior 를 고정한 게 리팩터링에 안전하다.
 */
object TwoProcessModelTest {

    private const val MIN_MS = 60_000L

    fun run() {
        oneHourCIsNotConstant()
        phaseRefKeepsMinuteResolution()
        cStaysWithinAmplitude()
        sIsIndependentOfEpochInterval()
    }

    /**
     * [핵심 회귀] 한 시간 안에서 C 가 값마다 달라야 한다.
     *
     * 버그 상태에서는 같은 시(hour) 안의 모든 epoch 이 같은 clockH 를 얻어
     * C 가 전부 한 값으로 뭉개진다 → 서로 다른 값이 1 개가 된다.
     *
     * ★ 왜 "서로 다른 값 개수 = 칸 수" 로 안 잡는가 (이 테스트를 두 번 고치면서 배운 것)
     *   C = A·cos(2π(clockH − phaseRef)/24) 이고 phaseRef 는 **구간의 정중앙**이다.
     *   한 수면 구간은 자기 중앙 기준 대칭이라 앞뒤가 같은 C 를 갖는다.
     *   60칸짜리를 세니 30쌍이 생겨 30 개만 나왔다. 03:39 로 잘라 "비대칭"을 만들려 했으나
     *   phaseRef 도 같이 정중앙으로 따라와 여전히 대칭이었다(40칸 → 20개).
     *   → 대칭은 구조적으로 못 없애므로 "전부 다르다"는 주장을 테스트에 넣으면 안 된다.
     *
     *   대신 **진짜 변해야 하는 것**을 본다:
     *     ① 중앙 쪽으로 갈수록 |위상| 이 줄어들므로 앞쪽 절반의 C 는 단조 증가한다 (버그면 전부 같아 단조가 깨진다)
     *     ② 1분 떨어진 두 칸의 C 가 다르다
     *   둘 다 버그에서 확실히 깨지고, 정상에서 확실히 성립한다.
     */
    private fun oneHourCIsNotConstant() {
        T.group("TwoProcessModel — clockHours 회귀 (한 시간 안에서 C 가 변해야 한다)")

        val start = Kst.ms("2026-09-14 03:00:00")
        val timestamps = Kst.range(start, start + 39 * MIN_MS, MIN_MS)   // 03:00~03:39, 40칸
        val out = TwoProcessModel.compute(timestamps, timestamps.map { true })
        val c = out.history.map { it.C }

        T.assertEquals("history 크기가 입력과 같음", timestamps.size, out.history.size)

        val distinct = c.distinct().size
        T.assertTrue("서로 다른 C 값이 $distinct 개 (>= 20, 대칭이라 절반)", distinct >= 20,
            "시(hour) 안의 모든 epoch 가 같은 clockH 를 받았다 = minute/second 를 버린 회귀. " +
                    "버그 상태면 distinct 가 1 이 된다.")

        // ① 앞쪽 절반(03:00→03:19) 은 phaseRef(03:19:30) 으로 다가가므로 |위상| 이 줄고 C 는 커진다.
        val half = c.size / 2
        val strictlyIncreasing = (1 until half).all { c[it] > c[it - 1] }
        T.assertTrue("앞쪽 절반의 C 가 엄격히 단조 증가한다 (phaseRef 쪽으로 접근)", strictlyIncreasing,
            "앞쪽 절반에서 C 가 단조 증가하지 않는다. clockH 가 분 단위로 움직이지 않는 것일 수 있다.")

        // ② 1분 떨어진 두 칸이 달라야 한다 — 버그를 가장 직접적으로 잡는 지표
        T.assertNotSame("03:00 과 03:01 의 C 가 다르다", c[0], c[1],
            "같은 시(hour) 안에서 1분 차이인데 C 가 같다 = clockHours 가 분을 버리고 있다.")

        // 30초 격자에서도 같은 확인
        val ts30 = Kst.range(start, start + 39 * 2 * MIN_MS + 30_000L, 30_000L)
        val c30 = TwoProcessModel.compute(ts30, ts30.map { true }).history.map { it.C }
        T.assertNotSame("30초 격자에서도 30초 차이 두 칸의 C 가 다르다", c30[0], c30[1],
            "30초 격자에서도 분/초를 버리고 있다.")
    }

    /**
     * [핵심 회귀] phaseRef 가 정수로 붙지 않아야 한다.
     *
     * 수면 04:00~04:59 한 구간이면 bout 중간 시각은 04:29:30 → clockH 4.4917 이다.
     * 버그 상태에서는 그 시각의 "시" 만 남아 4.0 이 된다.
     */
    private fun phaseRefKeepsMinuteResolution() {
        T.group("TwoProcessModel — phaseRef 가 분 단위 분해능을 유지")

        val start = Kst.ms("2026-09-14 04:00:00")
        val timestamps = Kst.range(start, start + 59 * MIN_MS, MIN_MS)
        val out = TwoProcessModel.compute(timestamps, timestamps.map { true })

        val expected = 4.0 + 29.5 / 60.0          // 04:29:30 → 4.491667
        T.assertClose("phaseRef 가 bout 중간 시각을 반영", expected, out.phaseRef, eps = 1e-6,
            why = "버그 상태면 시(4)만 남아 4.0 이 된다. 분 단위 분해능이 사라진 것.")
        T.assertTrue(
            "phaseRef 가 정수가 아니다",
            Math.abs(out.phaseRef - Math.floor(out.phaseRef)) > 1e-6,
            "phaseRef 가 정수로 붙었다 = bout 중간 시각이 정시로 뭉개졌다."
        )
    }

    /**
     * 불변식 (회귀 테스트가 아니라 안전망).
     *
     * S 는 0~1, C 는 진폭 0.15 안이어야 한다. 이건 어떤 버그에서도 지켜져야 한다.
     * 진동은 바뀌어도 범위를 벗어나면 안 된다.
     */
    private fun cStaysWithinAmplitude() {
        T.group("TwoProcessModel — S/C 범위 불변식")

        val amplitude = 0.15
        val base = Kst.ms("2026-09-14 00:00:00")
        val timestamps = Kst.range(base, base + 1439 * MIN_MS, MIN_MS)   // 24시간
        val out = TwoProcessModel.compute(timestamps, timestamps.map { true })

        val maxAbsC = out.history.maxOf { Math.abs(it.C) }
        T.assertTrue(
            "|C| 최대값이 진폭 이하 — ${fmt(maxAbsC)}",
            maxAbsC <= amplitude + 1e-9,
            "진폭 0.15 를 넘었다. offset 이 들어갔거나 진폭 파라미터가 어긋났다."
        )
        T.assertTrue(
            "|C| 가 진폭에 닿는다 — ${fmt(maxAbsC)}",
            maxAbsC >= amplitude - 1e-6,
            "24시간을 다 훑었는데 진폭 극값에 못 미쳤다. clockH 가 뭉개졌거나 " +
                    "phaseRef 계산이 어긋났을 때 나올 수 있다."
        )
        T.assertTrue(
            "S 가 0~1 — ${fmt(out.currentS)}",
            out.currentS in 0.0..1.0,
            "currentS 가 0~1 밖이다. S_LOWER/S_UPPER 파라미터가 깨졌다."
        )
    }

    /**
     * Process S 는 epoch 간격에 무관해야 한다.
     *
     * S 는 dt 를 실제 timestamp 차이로 계산하므로, **같은 절대 시간 구간**을 두 격자로
     * 덮으면 같은 값이 나와야 한다. exp(-dt/τ) 를 두 번 합성하면 한 번이 되기 때문이다.
     *
     * 이게 epoch 30초→1분 전환이 "안전하다"는 근거를 테스트로 고정한다.
     *
     * 주의 — 두 타임라인의 **끝 시각이 같아야 한다.**
     *   처음에 30초를 240칸(끝 7170초), 1분을 120칸(끝 7140초)으로 잡아서 실패했다.
     *   끝이 30초 차이 나면 "깨어 있는 꼬리" 길이가 달라져 S 가 0.0007 어긋난다.
     *   그건 코드가 틀린 게 아니라 비교 조건이 잘못된 거다. 그래도 조용히 어긋나는
     *   유형이라, 같은 실수 안 하게 끝 시각을 30초/60초 모두 나누어 떨어지게 잡았다.
     */
    private fun sIsIndependentOfEpochInterval() {
        T.group("TwoProcessModel — S 가 epoch 간격에 무관")

        val base = Kst.ms("2026-09-14 02:00:00")
        val endMs = base + 6_000_000L          // 6,000,000ms = 100분 = 30초 200칸 = 1분 100칸
        val sleepFromMs = 30 * MIN_MS          // 02:30 부터 수면

        val ts30 = Kst.range(base, endMs, 30_000L)
        val sleep30 = ts30.map { it >= base + sleepFromMs }

        val ts60 = Kst.range(base, endMs, MIN_MS)
        val sleep60 = ts60.map { it >= base + sleepFromMs }

        T.assertEquals("30초 타임라인 끝이 동일", endMs, ts30.last())
        T.assertEquals("1분 타임라인 끝이 동일", endMs, ts60.last())
        T.assertEquals("30초 칸 수 (100분)", 201, ts30.size)
        T.assertEquals("1분 칸 수 (100분)", 101, ts60.size)

        val s30 = TwoProcessModel.compute(ts30, sleep30).currentS
        val s60 = TwoProcessModel.compute(ts60, sleep60).currentS

        T.assertClose(
            "30초 S 와 1분 S 가 같다 — 30초=${fmt(s30)} 1분=${fmt(s60)}",
            s30, s60, eps = 1e-9,
            why = "S 는 dt 를 실제 timestamp 차이로 계산하므로 같은 절대 구간·같은 수면 시간이면 같은 값이어야 한다."
        )
    }

    private fun fmt(v: Double): String = String.format(Locale.US, "%.8f", v)
}