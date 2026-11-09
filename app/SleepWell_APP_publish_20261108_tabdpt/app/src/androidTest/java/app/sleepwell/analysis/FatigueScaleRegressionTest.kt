package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import java.io.File

@Suppress("DEPRECATION")
class FatigueScaleRegressionTest : InstrumentationTestCase() {

    fun testLegacySevenPointMappingIsFrozenAndNewFivePointScaleIsIdentity() {
        assertEquals(listOf(1, 2, 2, 3, 4, 4, 5),
            (1..7).map { FatigueScale.toEngine(it, FatigueScale.LEGACY_MAX) })
        assertEquals((1..5).toList(),
            (1..5).map { FatigueScale.toEngine(it, FatigueScale.CURRENT_MAX) })
        assertEquals(3, NextNightModel.fatigueForEngine(4))
        assertEquals(4, NextNightModel.fatigueForEngine(4, FatigueScale.CURRENT_MAX))
        assertEquals("피로도 4/7 (이전 척도)", FatigueScale.displayText(4, 7))
        assertEquals("피로도 4/5", FatigueScale.displayText(4, 5))
        val unknown = FatigueScale.displayText(6, Int.MIN_VALUE)
        assertEquals("피로도 6 (척도 확인 필요)", unknown)
        assertFalse(unknown.contains(Int.MIN_VALUE.toString()))
    }

    fun testLegacyCsvDefaultsToSevenWithoutInferringFromRawValue() {
        val parsed = SurveyCsv.parse(legacyCsv(4))
        assertTrue(parsed.errors.joinToString(), parsed.errors.isEmpty())
        val day = parsed.days.single()
        assertEquals(4, day.fatigue)
        assertEquals(FatigueScale.LEGACY_MAX, day.fatigueScaleMax)
        assertEquals(3, FatigueScale.toEngine(day.fatigue, day.fatigueScaleMax))
        assertTrue(day.summary().contains("4/7 (이전 척도)"))

        val decimalInteger = SurveyCsv.parse(legacyCsvText("4.0"))
        assertTrue(decimalInteger.errors.joinToString(), decimalInteger.errors.isEmpty())
        assertEquals(4, decimalInteger.days.single().fatigue)
        val fractional = SurveyCsv.parse(legacyCsvText("4.5"))
        assertNull(fractional.days.single().fatigue)
        assertTrue(fractional.errors.any { it.contains("정수로 읽을 수 없어") })
        val notANumber = SurveyCsv.parse(legacyCsvText("NaN"))
        assertNull(notANumber.days.single().fatigue)
        assertTrue(notANumber.errors.any { it.contains("정수로 읽을 수 없어") })
    }

    fun testMixedScalesRoundTripWithoutChangingRawScores() {
        val legacy = SurveyCsv.parse(legacyCsv(6)).days.single()
        val current = legacy.copy(date = "2026-11-06", fatigue = 5,
            fatigueScaleMax = FatigueScale.CURRENT_MAX)
        val csv = SurveyStore.toCsv(listOf(legacy, current))
        val restored = SurveyCsv.parse(csv)
        assertTrue(restored.errors.joinToString(), restored.errors.isEmpty())
        assertEquals(listOf(6 to 7, 5 to 5), restored.days.map { it.fatigue to it.fatigueScaleMax })
        assertNull(current.featureValues()["fatigueScore"])
        assertEquals(6.0, legacy.featureValues()["fatigueScore"])
        assertEquals(1.0, current.featureValues()["_surveyResponsePresent"])
        assertEquals(5, FatigueScale.toEngine(current.fatigue, current.fatigueScaleMax))
    }

    fun testInvalidKnownAndUnknownScalesKeepRawValueButAreExcludedFromModel() {
        val header = SurveyStore.HEADER.joinToString(",")
        fun row(scale: String) = SurveyStore.HEADER.joinToString(",") { name -> when (name) {
            "participant" -> "me"; "date" -> "2026-11-05"; "fatigue" -> "6"
            "fatigueScaleMax" -> scale; else -> ""
        } }
        listOf("5", "9", "unknown", "").forEach { scale ->
            val parsed = SurveyCsv.parse("$header\n${row(scale)}\n")
            assertEquals(6, parsed.days.single().fatigue)
            assertNull(FatigueScale.toEngine(parsed.days.single().fatigue, parsed.days.single().fatigueScaleMax))
            assertTrue("scale=$scale", parsed.errors.any { it.contains("모델 입력에서는 제외") })
        }
    }

    fun testFirstWriteBacksUpLegacyCsvOnceAndDoesNotOverwriteBackup() {
        val file = File(instrumentation.targetContext.cacheDir, "fatigue-scale-${System.nanoTime()}.csv")
        val backup = File(file.parentFile, file.nameWithoutExtension + ".before-fatigue-scale-v2.csv")
        try {
            val original = legacyCsv(7)
            file.writeText(original)
            val legacy = SurveyStore.load(file).single()
            assertFalse(backup.exists())
            SurveyStore.save(file, legacy, 1L)
            assertEquals(original, backup.readText())
            assertTrue(file.readLines().first().contains("fatigueScaleMax"))
            backup.writeText("protected backup")
            SurveyStore.save(file, legacy.copy(fatigue = 6), 2L)
            assertEquals("protected backup", backup.readText())
            val restored = SurveyStore.load(file).single()
            assertEquals(6, restored.fatigue)
            assertEquals(FatigueScale.LEGACY_MAX, restored.fatigueScaleMax)
        } finally {
            file.delete()
            backup.delete()
        }
    }

    fun testInvalidBackupTargetStopsWriteAndPreservesLegacySource() {
        val file = File(instrumentation.targetContext.cacheDir, "fatigue-backup-failure-${System.nanoTime()}.csv")
        val backup = File(file.parentFile, file.nameWithoutExtension + ".before-fatigue-scale-v2.csv")
        try {
            val original = legacyCsv(3)
            file.writeText(original)
            assertTrue(backup.mkdir())
            assertFails { SurveyStore.save(file, SurveyStore.load(file).single(), 1L) }
            assertEquals(original, file.readText())
            assertTrue(backup.isDirectory)
        } finally {
            file.delete()
            backup.delete()
        }
    }

    private fun legacyCsv(fatigue: Int): String =
        "participant,date,fatigue\nme,2026-11-05,$fatigue\n"

    private fun legacyCsvText(fatigue: String): String =
        "participant,date,fatigue\nme,2026-11-05,$fatigue\n"

    private fun assertFails(block: () -> Unit) {
        try {
            block()
            fail("예외가 발생해야 합니다.")
        } catch (_: IllegalStateException) {
            // expected
        }
    }
}
