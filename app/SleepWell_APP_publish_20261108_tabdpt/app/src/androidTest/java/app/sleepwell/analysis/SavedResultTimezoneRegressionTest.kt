package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.PredictionOrigin
import java.io.File
import java.time.Instant
import java.util.TimeZone

@Suppress("DEPRECATION")
class SavedResultTimezoneRegressionTest : InstrumentationTestCase() {
    fun testRawSavedScheduleRendersInCurrentTimezone() {
        val row = rawRow()

        val utc = ResultStore.render(row, TimeZone.getTimeZone("UTC"))

        assertEquals("기준 11/3(화) 11:00 기상", utc.basisText)
        assertEquals("11/3 14:00 취침 → 11/3 22:00 기상", utc.modelText)
        assertEquals("11/3 22:00 기상 목표 → 11/3 13:00 전에 취침 · 권장 수면 기회 8시간", utc.recommendationText)
        assertEquals(
            "23:00 취침 → 07:00 기상 (Asia/Seoul 기준 반복 시각 · 자동 변환 안 함)",
            utc.habitText
        )
        assertEquals("당시 저장된 예측 · 현재 UTC 표시 · 저장 당시 Asia/Seoul", utc.provenanceText)
    }

    fun testNewYorkOvernightHabitAcrossDstRemainsTruthfulClockStatistic() {
        val row = rawRow().copy(
            sourceZoneId = "America/New_York",
            basisMs = epoch("2026-03-08T01:00:00Z"), // 3/7 20:00 EST, DST changes overnight.
            habitText = "23:00 취침 → 07:00 기상",
            habitBedMinute = 23 * 60,
            habitWakeMinute = 7 * 60
        )

        val utc = ResultStore.render(row, TimeZone.getTimeZone("UTC"))

        assertEquals(
            "23:00 취침 → 07:00 기상 (America/New_York 기준 반복 시각 · 자동 변환 안 함)",
            utc.habitText
        )
        assertFalse(utc.habitText.contains("04:00"))
        assertFalse(utc.habitText.contains("11:00"))
    }

    fun testRawTimezoneFieldsSurviveCsvRoundTrip() {
        val file = File.createTempFile("saved-result-zone", ".csv")
        try {
            ResultStore.save(file, rawRow())

            val restored = ResultStore.load(file).single()

            assertEquals("Asia/Seoul", restored.sourceZoneId)
            assertEquals(epoch("2026-11-03T14:00:00Z"), restored.modelBedMs)
            assertEquals(epoch("2026-11-03T22:00:00Z"), restored.modelWakeMs)
            assertEquals(epoch("2026-11-03T13:00:00Z"), restored.recommendedBedMs)
            assertEquals(epoch("2026-11-03T22:00:00Z"), restored.recommendedWakeMs)
            assertEquals(23 * 60, restored.habitBedMinute)
            assertEquals(7 * 60, restored.habitWakeMinute)
            assertEquals(epoch("2026-11-02T14:30:00Z"), restored.actualBedMs)
            assertEquals(epoch("2026-11-02T22:30:00Z"), restored.actualWakeMs)
            assertEquals(PredictionOrigin.LIVE, restored.predictionOrigin)
            assertNull(restored.historyCutoffMs)
        } finally {
            file.delete()
        }
    }

    fun testPastActualTimesRenderFromRawInstantsInCurrentTimezone() {
        val row = rawRow().copy(
            isPast = true,
            actualText = "11/2 23:30 취침 → 11/3 07:30 기상 · 예상보다 30분 늦게 잠들었어요",
            actualBedMs = epoch("2026-11-02T14:30:00Z"),
            actualWakeMs = epoch("2026-11-02T22:30:00Z")
        )

        val utc = ResultStore.render(row, TimeZone.getTimeZone("UTC"))

        assertEquals(
            "11/2 14:30 취침 → 11/2 22:30 기상 · 예상보다 30분 늦게 잠들었어요",
            utc.actualText
        )
    }

    fun testLegacyPastActualStringKeepsStoredZoneLabel() {
        val row = rawRow().copy(
            isPast = true, actualText = "11/2 23:30 취침 → 11/3 07:30 기상",
            actualBedMs = null, actualWakeMs = null
        )

        val utc = ResultStore.render(row, TimeZone.getTimeZone("UTC"))

        assertEquals("11/2 23:30 취침 → 11/3 07:30 기상 (Asia/Seoul 저장 문자열)", utc.actualText)
    }

    fun testLegacyStringOnlyRowIsNotReinterpretedAsAnInstant() {
        val file = File.createTempFile("saved-result-legacy", ".csv")
        try {
            file.writeText(
                "savedAtMs,source,basisMs,basisText,modelText,recText,habitText,statusText,surveyText,S,C,propensity\n" +
                    "1,legacy,0,기준 10/24 20:00,10/24 23:00 KST 취침 → 10/25 07:00 KST 기상,—,23:00 취침 → 07:00 기상,status,,,,\n"
            )

            val restored = ResultStore.load(file).single()
            val rendered = ResultStore.render(restored, TimeZone.getTimeZone("UTC"))

            assertNull(restored.sourceZoneId)
            assertEquals(restored.modelText, rendered.modelText)
            assertEquals(restored.habitText, rendered.habitText)
            assertEquals(
                "저장된 결과 · 생성 방식 미기록 · 저장 당시 KST 표시 문자열",
                rendered.provenanceText
            )
            assertEquals(PredictionOrigin.LEGACY_UNSPECIFIED, restored.predictionOrigin)
        } finally {
            file.delete()
        }
    }

    private fun rawRow() = SavedResult(
        savedAtMs = epoch("2026-11-03T11:05:00Z"),
        source = "직접 수면일지",
        basisMs = epoch("2026-11-03T11:00:00Z"),
        basisText = "기준 11/3(화) 20:00 기상",
        modelText = "11/3 23:00 취침 → 11/4 07:00 기상",
        recText = "11/4 07:00 기상 목표 → 11/3 22:00 전에 취침 · 권장 수면 기회 8시간",
        habitText = "23:00 취침 → 07:00 기상",
        statusText = "status",
        surveyText = "",
        sValue = "",
        cValue = "",
        pValue = "",
        sourceZoneId = "Asia/Seoul",
        modelBedMs = epoch("2026-11-03T14:00:00Z"),
        modelWakeMs = epoch("2026-11-03T22:00:00Z"),
        recommendedBedMs = epoch("2026-11-03T13:00:00Z"),
        recommendedWakeMs = epoch("2026-11-03T22:00:00Z"),
        habitBedMinute = 23 * 60,
        habitWakeMinute = 7 * 60,
        recommendationSleepText = "권장 수면 기회 8시간",
        actualBedMs = epoch("2026-11-02T14:30:00Z"),
        actualWakeMs = epoch("2026-11-02T22:30:00Z"),
        predictionOrigin = PredictionOrigin.LIVE
    )

    private fun epoch(value: String): Long = Instant.parse(value).toEpochMilli()
}
