package test

import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.EpochInterval
import app.sleepwell.health.HealthResult
import app.sleepwell.health.SleepAnalysisPipeline
import java.io.File

/**
 * ★ 회귀 테스트 — SleepAnalysisPipeline (CSV → AnalysisResult)
 *
 * 특히 두 가지를 고정한다.
 *
 * 1. **applyDirectInput 의 격자 폭 (2026-09-27 고친 버그)**
 *      30초로 하드코딩돼 있던 `e.timestampMs + 30_000L` 이 1분 모델에서 남아 있으면
 *      사용자가 입력한 취침·기상 시각이 **절반 크기로만** 반영된다.
 *      멀쩡하게 실행되서 조용히 틀린다. 그래서 "간격을 바꿨을 때 결과가 실제로 달라지는지"를 본다.
 *
 * 2. **CSV 우선 규칙**
 *      직접 입력이 CSV 기록과 겹치면 CSV 가 이긴다. (합의안: CSV 우선)
 *
 * 합성 CSV 를 만들어 쓴다. 실제 혜지 데이터에 의존하지 않아야 테스트가 재현 가능하기 때문이다.
 */
object SleepAnalysisPipelineTest {

    /**
     * 02:00~06:00 KST 구간을 UTC 로 읽으면 11:00~15:00 KST 가 된다.
     * (CsvFileSource 가 SAMSUNG_TIMES_ARE_UTC = true 이므로)
     * 그래도 테스트는 **epoch ms 를 직접 계산**해서 기준 시각을 정하므로 시간대 해석에 흔들리지 않는다.
     *
     * 세션 S1 (한 밤):
     *   02:00~03:00 LIGHT
     *   03:00~04:00 WAKE   ← 직접 입력 "CSV 우선" 테스트에 쓴다
     *   04:00~06:00 LIGHT
     *   sleep_id 로 묶여 하나의 세션이다. sleepRecords 에는 세션 1개로 잡힌다.
     */
    private fun synthCsv(): File {
        val f = File.createTempFile("sleepwell-test", ".csv")
        f.deleteOnExit()
        val header = "start_time,end_time,duration_min,stage,stage_name,sleep_id,datauuid,time_offset"
        val rows = listOf(
            "2026-09-14 02:00:00,2026-09-14 03:00:00,60.0,40002,얕은 수면 (Light),S1,U1,UTC+0900",
            "2026-09-14 03:00:00,2026-09-14 04:00:00,60.0,40001,깨어있음 (Wake),S1,U2,UTC+0900",
            "2026-09-14 04:00:00,2026-09-14 06:00:00,120.0,40002,얕은 수면 (Light),S1,U3,UTC+0900"
        )
        f.writeText((listOf(header) + rows).joinToString("\n"), Charsets.UTF_8)
        return f
    }

    private fun run(csv: File, di: DirectInput?, iv: EpochInterval, refMs: Long?) =
        SleepAnalysisPipeline.analyze(csv.inputStream(), di, refMs, iv, true)

    // HealthResult.Success<T> 는 covariant 이므로 HealthResult.Success 로 캐스팅하면
    // data 가 Any? 로 보인다. 구체 타입을 붙여야 아래에서 필드가 열린다.
    private fun successOrNull(r: HealthResult<*>): AnalysisResult? =
        ((r as? HealthResult.Success)?.data) as? AnalysisResult

    fun run() {
        happyPath()
        directInputLowersSleepPressure()
        csvTakesPriorityOverDirectInput()
        epochIntervalChangesResult()
        recommendationIsProduced()
        insufficientDataIsRejected()
    }

    /** 파이프라인은 더 이상 recommendation=null을 내보내지 않는다. */
    private fun recommendationIsProduced() {
        T.group("SleepAnalysisPipeline — 추천 통합")
        val r = successOrNull(run(synthCsv(), null, EpochInterval.MIN1, null))
        T.assertTrue("추천 객체 생성", r?.recommendation != null, "recommendation=null")
        T.assertTrue(
            "학습 전에는 물리모델 fallback으로 명시",
            r?.recommendation?.modelStatus == "physics_only_untrained",
            "status=${r?.recommendation?.modelStatus}"
        )
    }

    /** 기본 경로: CSV 하나가 들어오면 S/C/propensity 가 나오고 세션이 1개 잡힌다. */
    private fun happyPath() {
        T.group("SleepAnalysisPipeline — 기본 경로")

        val csv = synthCsv()
        val res = run(csv, null, EpochInterval.MIN1, null)
        T.assertTrue("Success 반환", res is HealthResult.Success, "결과 타입=${res!!::class.java.simpleName}")

        val r = successOrNull(res)
        if (r == null) {
            T.fail("Success 면 AnalysisResult 가 있어야 한다", "null")
            return
        }
        T.assertEquals("세션 수", 1, r.sleepRecords.size)
        T.assertTrue("S 가 0~1", r.currentS in 0.0..1.0, "S=${r.currentS}")
        T.assertTrue("C 가 진폭 0.15 안", Math.abs(r.currentC) <= 0.15 + 1e-9, "C=${r.currentC}")
        T.assertEquals("epoch 간격 라벨이 결과에 기록됨", "1분", r.epochIntervalLabel)
        T.assertEquals("epoch 간격 ms 가 결과에 기록됨", 60_000L, r.epochIntervalMs)
        T.assertTrue("타임라인이 7일 창", r.inputPeriodEndMs - r.inputPeriodStartMs == 7L * 24 * 3600 * 1000,
            "입력 구간 길이=${r.inputPeriodEndMs - r.inputPeriodStartMs} (7일이 아님)")
        T.assertTrue("epoch 수가 7일/간격", r.scHistory.size == 7 * 24 * 60,
            "epoch 수=${r.scHistory.size}, 기대=${7 * 24 * 60}")

        // 같은 CSV 를 30초로 돌리면 칸 수가 정확히 두 배여야 한다.
        val r30 = successOrNull(run(csv, null, EpochInterval.SEC30, null))
        T.assertTrue(
            "30초는 1분의 정확히 두 배 — 1분=${r.scHistory.size} 30초=${r30?.scHistory?.size}",
            r30 != null && r30.scHistory.size == r.scHistory.size * 2,
            "간격만 바꿨는데 칸 수가 두 배가 아니면 격자 계산이 어긋난 것."
        )
    }

    /**
     * [핵심 회귀] 직접 입력이 격자 폭을 따라 반영된다.
     *
     * 세션 밖(깨어 있는 구간)에 낮잠을 입력하면 수면 압력이 내려가야 한다.
     * 그리고 30초/1분에서 반영되는 **시간**은 같아야 하고(칸 수만 다르다).
     */
    private fun directInputLowersSleepPressure() {
        T.group("SleepAnalysisPipeline — 직접 입력 반영")

        val csv = synthCsv()
        val base = successOrNull(run(csv, null, EpochInterval.MIN1, null))!!
        val refMs = base.inputPeriodEndMs

        // 세션 시작 5일 뒤 = 확실히 CSV 밖인 깨어 있는 구간
        val napStart = base.inputPeriodStartMs + 5L * 24 * 3600 * 1000
        val napEnd = napStart + 4 * 3600 * 1000
        val di = DirectInput(napStart, napEnd, null, null)

        val withNap = successOrNull(run(csv, di, EpochInterval.MIN1, refMs))
        if (withNap == null) {
            T.fail("직접 입력 후에도 Success 여야 한다", "null")
            return
        }

        T.assertTrue(
            "직접 입력 후 S 가 내려간다 — ${base.currentS} -> ${withNap.currentS}",
            withNap.currentS < base.currentS,
            "4시간 낮잠을 입력했는데 수면 압력이 줄지 않았다 = 직접 입력이 반영되지 않았다.")
        T.assertEquals("epoch 수는 그대로 (칸만 뒤집음)", base.scHistory.size, withNap.scHistory.size)

        // 30초에서도 같은 길이가 반영돼야 한다. 반쪽만 반영되면 격자 폭 문제다.
        val base30 = successOrNull(run(csv, null, EpochInterval.SEC30, refMs))!!
        val withNap30 = successOrNull(run(csv, di, EpochInterval.SEC30, refMs))!!
        T.assertTrue(
            "30초에서도 S 가 내려간다 — ${base30.currentS} -> ${withNap30.currentS}",
            withNap30.currentS < base30.currentS,
            "30초 모델에서 직접 입력이 안 반영됐다.")

        // 두 격자가 같은 물리량을 말하는지: 4시간 수면이 만들어 낸 S 하강 폭이 비슷해야 한다.
        val dropMin = base.currentS - withNap.currentS
        val drop30 = base30.currentS - withNap30.currentS
        T.assertTrue(
            "S 하강 폭이 두 격자에서 비슷하다 — 1분=$dropMin 30초=$drop30",
            Math.abs(dropMin - drop30) < 0.02,
            "격자에 따라 반영된 수면 시간이 달라졌다. 하드코딩 30_000L 이 남아 있을 수 있다.")
    }

    /** [핵심] CSV 기록과 겹치면 CSV 가 이긴다. */
    private fun csvTakesPriorityOverDirectInput() {
        T.group("SleepAnalysisPipeline — CSV 우선 규칙")

        val csv = synthCsv()
        val base = successOrNull(run(csv, null, EpochInterval.MIN1, null))!!
        val refMs = base.inputPeriodEndMs

        // 세션 안의 WAKE 구간(03:00~04:00 UTC = KST 12:00~13:00)을 전부 덮는다.
        val sessionStartMs = base.sleepRecords.first().startMs
        val wakeStart = sessionStartMs + 1 * 3600 * 1000    // 03:00 UTC
        val wakeEnd = wakeStart + 1 * 3600 * 1000          // 04:00 UTC
        val di = DirectInput(wakeStart, wakeEnd, null, null)

        val res = successOrNull(run(csv, di, EpochInterval.MIN1, refMs))
        if (res == null) {
            T.fail("CSV 기록 위의 직접 입력도 Success 여야 한다", "null")
            return
        }
        T.assertClose(
            "CSV 가 이겨서 S 가 그대로다",
            base.currentS, res.currentS, eps = 1e-12,
            why = "세션 안에 이미 기록(각성)이 있는데 그 위를 '수면'으로 덮었다. " +
                    "합의안의 CSV 우선 규칙이 깨졌다."
        )
    }

    /** 간격이 결과에 기록되는지 + 간격을 바꾸면 결과가 실제로 달라지는지. */
    private fun epochIntervalChangesResult() {
        T.group("SleepAnalysisPipeline — 간격 인자가 결과에 반영")

        val csv = synthCsv()
        val r60 = successOrNull(run(csv, null, EpochInterval.MIN1, null))!!
        val r30 = successOrNull(run(csv, null, EpochInterval.SEC30, null))!!

        T.assertEquals("1분 라벨", "1분", r60.epochIntervalLabel)
        T.assertEquals("30초 라벨", "30초", r30.epochIntervalLabel)
        T.assertTrue("modelVersion 에 간격이 박혀 있다 (1분)",
            r60.modelVersion.contains("1분"), "modelVersion=${r60.modelVersion}")
        T.assertTrue("modelVersion 에 간격이 박혀 있다 (30초)",
            r30.modelVersion.contains("30초"), "modelVersion=${r30.modelVersion}")
        T.assertNotSame("간격을 바꾸면 S 값이 달라진다",
            r60.currentS, r30.currentS,
            "간격이 다른데 S 가 완전히 같으면 epoch 가 반영되지 않는 것이다.")
    }

    /** 수면/각성 한쪽뿐이면 "기록이 더 필요합니다" 로 거절해야 한다. */
    private fun insufficientDataIsRejected() {
        T.group("SleepAnalysisPipeline — 데이터 부족 처리")

        val csv = synthCsv()
        val r = run(csv, null, EpochInterval.MIN1, null)
        val ok = r is HealthResult.Success
        T.assertTrue("수면+각성 둘 다 있는 입력은 통과", ok, "통과해야 하는데 ${r::class.java.simpleName}")

        // 세션이 하나뿐이라 referenceTime 을 세션 시작보다 아주 앞서면 범위에 세션이 없다.
        val earlyRef = Kst.ms("2020-01-01 00:00:00")
        val r2 = run(csv, null, EpochInterval.MIN1, earlyRef)
        T.assertTrue("타임라인에 세션이 없으면 예외 대신 Unavailable",
            r2 is HealthResult.Unavailable,
            "세션이 없는데 Success 를 냈다 = 예외가 밖으로 나갔거나 검증이 없다. 타입=${r2::class.java.simpleName}")
    }
}
