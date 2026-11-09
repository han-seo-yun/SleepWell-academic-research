package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.SleepSession
import app.sleepwell.user.SleepInputPolicy
import app.sleepwell.user.SleepRecord
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import java.io.File
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.Locale

/** Integration coverage for the versioned raw fatigue scale at model boundaries. */
@Suppress("DEPRECATION")
class FatigueScalePersonalizationRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")
    private val evaluation = ZonedDateTime.of(2026, 11, 10, 21, 0, 0, 0, seoul)
    private val profile = UserProfile(
        ageBand = "30-44", sex = "any", activityMinutes = 60,
        usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
        createdAt = "2026-10-01T00:00:00Z"
    )

    fun testDirectManualDiaryUsesCurrentFivePointScaleWithoutSecondConversion() {
        val wake = ZonedDateTime.of(2026, 11, 9, 7, 0, 0, 0, seoul)
        fun parsed(raw: Int, scaleMax: Int = FatigueScale.CURRENT_MAX) = DirectInputParser.parse(
            DirectInputForm(
                bedText = "2026-11-08 23:00", wakeText = "2026-11-09 07:00",
                sleepLatencyText = "10", awakeMinutesText = "10", rested = 5,
                condition = raw, fatigueScaleMax = scaleMax
            ),
            nowMs = wake.plusHours(2).toInstant().toEpochMilli(), zoneId = seoul
        )

        val currentThree = parsed(3)
        val currentFour = parsed(4)
        val legacyFour = parsed(4, FatigueScale.LEGACY_MAX)

        assertTrue(currentThree.errors.joinToString(), currentThree.ok)
        assertEquals(3, currentThree.diary!!.fatigue)
        assertEquals(4, currentFour.diary!!.fatigue)
        assertEquals(3, legacyFour.diary!!.fatigue)
        assertEquals(3, currentThree.survey!!.fatigue)
        assertEquals(FatigueScale.CURRENT_MAX, currentThree.survey!!.fatigueScaleMax)
        assertEquals(4, legacyFour.survey!!.fatigue)
        assertEquals(FatigueScale.LEGACY_MAX, legacyFour.survey!!.fatigueScaleMax)
    }

    fun testSameRawFourHasDifferentGoodNightMeaningOnlyAfterScaleAwareBoundary() {
        val legacyDir = isolatedDir("legacy-four")
        val currentDir = isolatedDir("current-four")
        try {
            val legacy = oneJoinedNight(legacyDir, "2026-11-09", rawFatigue = 4,
                scaleMax = FatigueScale.LEGACY_MAX)
            val current = oneJoinedNight(currentDir, "2026-11-09", rawFatigue = 4,
                scaleMax = FatigueScale.CURRENT_MAX)

            assertEquals(3, legacy.fatigue)
            assertEquals(4, current.fatigue)
            val engine = UserModelEngine(instrumentation.targetContext)
            val legacyResult = engine.compute(profile, listOf(legacy), evaluation.toInstant(),
                inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1)
            val currentResult = engine.compute(profile, listOf(current), evaluation.toInstant(),
                inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1)

            assertEquals(1, legacyResult.outcomes.getJSONObject("personalization").getInt("good_night_count"))
            assertEquals(0, currentResult.outcomes.getJSONObject("personalization").getInt("good_night_count"))
        } finally {
            legacyDir.deleteRecursively()
            currentDir.deleteRecursively()
        }
    }

    fun testMixedScalesKeepOneRecordPerSleepAndPreserveCausalQuestionnaireTimes() {
        val dir = isolatedDir("mixed-scales")
        try {
            ingestNight(dir, "2026-11-08")
            ingestNight(dir, "2026-11-09")
            val legacy = survey("2026-11-08", 4, FatigueScale.LEGACY_MAX)
            val current = survey("2026-11-09", 4, FatigueScale.CURRENT_MAX)
            val loaded = ImportedSleepStore.loadModelNights(
                dir, evaluation.toInstant().toEpochMilli(), listOf(legacy, current)
            )

            assertEquals(2, loaded.size)
            assertEquals(setOf("2026-11-08", "2026-11-09"), loaded.map { it.day }.toSet())
            assertEquals(3, loaded.single { it.day == "2026-11-08" }.fatigue)
            assertEquals(4, loaded.single { it.day == "2026-11-09" }.fatigue)
            assertEquals(legacy.savedAt, loaded.single { it.day == "2026-11-08" }.subjectiveObservedAt)
            assertEquals(current.savedAt, loaded.single { it.day == "2026-11-09" }.subjectiveObservedAt)

            val outcome = UserModelEngine(instrumentation.targetContext).compute(
                profile, loaded, evaluation.toInstant(),
                inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
            ).outcomes.getJSONObject("personalization")
            assertEquals(0, outcome.getInt("eligible_primary_nights")) // wearable fixtures are not manual diaries
            assertEquals(2, outcome.getInt("eligible_input_nights"))
            assertEquals(1, outcome.getInt("good_night_count"))
        } finally { dir.deleteRecursively() }
    }

    fun testLegacySevenPointPathMatchesPriorNormalizedEngineResults() {
        val dir = isolatedDir("legacy-equivalence")
        try {
            val surveys = (3..9).map { day ->
                val wakeDate = String.format(Locale.US, "2026-11-%02d", day)
                ingestNight(dir, wakeDate)
                survey(wakeDate, rawFatigue = 4, scaleMax = FatigueScale.LEGACY_MAX)
            }
            val joined = ImportedSleepStore.loadModelNights(
                dir, evaluation.toInstant().toEpochMilli(), surveys
            )
            assertEquals(7, joined.size)
            assertTrue(joined.all { it.fatigue == 3 })

            val priorNormalized = joined.map { record -> record.copy(fatigue = 3) }
            val engine = UserModelEngine(instrumentation.targetContext)
            val migrated = engine.compute(profile, joined, evaluation.toInstant(),
                inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1)
            val baseline = engine.compute(profile, priorNormalized, evaluation.toInstant(),
                inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1)

            val migratedPersonalization = migrated.outcomes.getJSONObject("personalization")
            val baselinePersonalization = baseline.outcomes.getJSONObject("personalization")
            assertEquals(7, migratedPersonalization.getInt("good_night_count"))
            assertTrue(migratedPersonalization.getDouble("sleep_target_weight") > 0.0)
            assertEquals(baselinePersonalization.toString(), migratedPersonalization.toString())
            assertEquals(baseline.recommendation.durationMinutes, migrated.recommendation.durationMinutes)
            assertEquals(baseline.recommendation.bedtime, migrated.recommendation.bedtime)
            assertEquals(baseline.recommendation.wakeTime, migrated.recommendation.wakeTime)
        } finally { dir.deleteRecursively() }
    }

    private fun oneJoinedNight(dir: File, wakeDate: String, rawFatigue: Int, scaleMax: Int): SleepRecord {
        ingestNight(dir, wakeDate)
        return ImportedSleepStore.loadModelNights(
            dir, evaluation.toInstant().toEpochMilli(), listOf(survey(wakeDate, rawFatigue, scaleMax))
        ).single()
    }

    private fun ingestNight(dir: File, wakeDate: String) {
        val wake = java.time.LocalDate.parse(wakeDate).atTime(7, 0).atZone(seoul)
        ImportedSleepStore.ingest(
            dir,
            listOf(SleepSession("watch:$wakeDate", wake.minusHours(8).toInstant().toEpochMilli(),
                wake.toInstant().toEpochMilli())),
            "health_connect", wake.plusHours(1).toInstant().toEpochMilli()
        )
    }

    private fun survey(wakeDate: String, rawFatigue: Int, scaleMax: Int): SurveyDay {
        val wake = java.time.LocalDate.parse(wakeDate).atTime(7, 0).atZone(seoul)
        return SurveyDay(
            participant = "me", date = wakeDate, caffeineUsed = null, caffeineMg = null,
            lastCaffeine = null, napMinutes = null, napEnd = null, exerciseMinutes = null,
            exerciseEnd = null, exerciseIntensity = null, fatigue = rawFatigue, steps = null,
            heartRate = null, alarmTime = null, firstCommitment = null, examTomorrow = false,
            alcohol = false, illness = false, savedAt = wake.plusHours(2).toInstant().toString(),
            rested = 5, sleepWakeDate = wakeDate, sleepLatencyMinutes = 10, awakeMinutes = 20,
            sleepWakeMs = wake.toInstant().toEpochMilli(), fatigueScaleMax = scaleMax
        )
    }

    private fun isolatedDir(label: String): File = File(
        instrumentation.targetContext.cacheDir,
        "fatigue-scale-personalization-$label-${System.nanoTime()}"
    ).also { assertTrue(it.mkdirs()) }
}
