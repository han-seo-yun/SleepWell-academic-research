package test

import app.sleepwell.health.EpochGrid
import app.sleepwell.health.EpochInterval

/**
 * ★ 회귀 테스트 — epoch 격자 경계 정렬 (EpochInterval / EpochGrid)
 *
 * 왜 이게 중요한가:
 *   기준 모델 TwoProcessModel 이 epoch 시각의 **시계 시각**(시 + 분/60 + 초/3600)을
 *   C_sleep 에 그대로 넣는다. 타임라인 시작이 epoch 경계에 안 떨어지면 모든 epoch 가
 *   예컨대 :30 에서 시작하므로 분/60 이 0.5 만큼 어긋나고 C 가 최대 interval/2 만큼 밀린다.
 *   30초면 15초, 1분이면 30초 차 — 하루 리듬 위상 계산에 그대로 남는다.
 *
 *   ceilTo 는 지금 어디에도 쓰이지 않지만, "내림/올림" 을 한 쌍으로 두는 게 정상이므로
 *   고정해 둔다. 나중에 경계 근처 값을 올릴 때 쓰게 된다.
 */
object EpochGridTest {

    fun run() {
        floorToBasics()
        ceilToBasics()
        isAligned()
        negativeTimestamps()
        pairProperties()
    }

    private fun floorToBasics() {
        T.group("EpochGrid.floorTo — 내림")

        T.assertEquals("0 은 그대로", 0L, EpochGrid.floorTo(0L, EpochInterval.SEC30))
        T.assertEquals("29.999초 → 0 (30초)", 0L, EpochGrid.floorTo(29_999L, EpochInterval.SEC30))
        T.assertEquals("30초 → 30초 (30초)", 30_000L, EpochGrid.floorTo(30_000L, EpochInterval.SEC30))
        T.assertEquals("59.999초 → 30초 (30초)", 30_000L, EpochGrid.floorTo(59_999L, EpochInterval.SEC30))
        T.assertEquals("1초 → 0 (1분)", 0L, EpochGrid.floorTo(1_000L, EpochInterval.MIN1))
        T.assertEquals("59.999초 → 0 (1분)", 0L, EpochGrid.floorTo(59_999L, EpochInterval.MIN1))
        T.assertEquals("60초 → 60초 (1분)", 60_000L, EpochGrid.floorTo(60_000L, EpochInterval.MIN1))

        // 경계가 1분이어야 초·분이 0으로 떨어진다. 이게 전제다.
        val t = 1_789_500_123_456L   // 임의의 큰 값
        val f = EpochGrid.floorTo(t, EpochInterval.MIN1)
        T.assertEquals("floor 결과가 원본 이하", true, f <= t)
        T.assertEquals("floor 결과가 interval 미만이 아님", true, t - f < EpochInterval.MIN1.intervalMs)
    }

private fun ceilToBasics() {
        T.group("EpochGrid.ceilTo — 올림")

        T.assertEquals("이미 경계면 그대로 (30초)", 30_000L, EpochGrid.ceilTo(30_000L, EpochInterval.SEC30))
        T.assertEquals("1ms 는 다음 경계로 (30초)", 60_000L, EpochGrid.ceilTo(30_001L, EpochInterval.SEC30),
            why = "30000 경계는 이미 지났으니 그다음인 60000 이.ceil 은 '바로 다음 경계' 다.")
        T.assertEquals("29.999초 → 30초 (30초)", 30_000L, EpochGrid.ceilTo(29_999L, EpochInterval.SEC30))
        T.assertEquals("1ms → 30초 (30초)", 30_000L, EpochGrid.ceilTo(1L, EpochInterval.SEC30))
        T.assertEquals("0 은 그대로 (1분)", 0L, EpochGrid.ceilTo(0L, EpochInterval.MIN1))
        T.assertEquals("1초 → 1분 (1분)", 60_000L, EpochGrid.ceilTo(1_000L, EpochInterval.MIN1))
    }

    private fun isAligned() {
        T.group("EpochGrid.isAligned — 경계 판정")

        T.assertTrue("정시(00:00)는 1분 경계", EpochGrid.isAligned(0L, EpochInterval.MIN1))
        T.assertFalse("1ms 는 1분 경계 아님", EpochGrid.isAligned(1L, EpochInterval.MIN1))
        T.assertFalse("30초는 1분 경계 아님", EpochGrid.isAligned(30_000L, EpochInterval.MIN1))
        T.assertTrue("30초는 30초 경계", EpochGrid.isAligned(30_000L, EpochInterval.SEC30))
        T.assertFalse("29.999초는 30초 경계 아님", EpochGrid.isAligned(29_999L, EpochInterval.SEC30))
    }

    /**
     * 1970 이전 시각. Math.floorDiv 가 아니라 floorMod 를 써야 음수에서 뒤집히지 않는다.
     * 수면 데이터는 1970 이후라 실제로 안 터지지만, 엣지 케이스로 고정해 둔다.
     */
    private fun negativeTimestamps() {
        T.group("EpochGrid — 음수(1970 이전) 시각")

        T.assertEquals("음수 floorTo (1분)", -120_000L, EpochGrid.floorTo(-61_000L, EpochInterval.MIN1))
        T.assertEquals("음수 floorTo (30초)", -30_000L, EpochGrid.floorTo(-1L, EpochInterval.SEC30))
        T.assertTrue("음수 ceilTo 가 원본 이상", EpochGrid.ceilTo(-1L, EpochInterval.MIN1) >= -1L)
        T.assertTrue("음수 floorTo 가 원본 이하", EpochGrid.floorTo(-1L, EpochInterval.MIN1) <= -1L)
    }

    /**
     * 경계가 **아닌** 시각에 대해서만 ceil - floor 가 정확히 간격 크기여야 한다.
     *
     * 경계 위의 시각은 floor 와 ceil 이 같으므로 차이가 0 이다. 그건 정답이다.
     * (이 성질을 처음에 "항상"으로 써서 경계 샘플에서 실패했다. 테스트를 고친 게 아니라
     *  성질의 전제를 정확히 적은 것.)
     */
    private fun pairProperties() {
        T.group("EpochGrid — floor/ceil 짝 성질 (경계 아닌 시각)")

        val samples = listOf(1L, 29_999L, 59_999L, 1_700_000_000_001L)
        var allOk = true
        var bad = ""
        var checked = 0
        for (i in EpochInterval.entries) {
            for (t in samples) {
                if (EpochGrid.isAligned(t, i)) continue
                checked++
                val f = EpochGrid.floorTo(t, i)
                val c = EpochGrid.ceilTo(t, i)
                if (c - f != i.intervalMs) {
                    allOk = false
                    bad = "간격 ${i.label} 시각 $t 에서 ceil-floor = ${c - f} (기대 ${i.intervalMs})"
                }
            }
        }
        T.assertEquals("검사한 샘플 수", 8, checked)
        T.assertTrue("ceil - floor 가 항상 간격 크기", allOk, bad)

        // 경계 위에서는 floor == ceil 이어야 한다.
        var alignedOk = true
        var alignedBad = ""
        for (i in EpochInterval.entries) {
            for (k in 0L..3L) {
                val t = k * i.intervalMs
                if (EpochGrid.floorTo(t, i) != t || EpochGrid.ceilTo(t, i) != t) {
                    alignedOk = false
                    alignedBad = "간격 ${i.label} 경계 $t 에서 floor/ceil 이 그대로가 아니다"
                }
            }
        }
        T.assertTrue("경계 위에서는 floor == ceil == 원본", alignedOk, alignedBad)
    }
}