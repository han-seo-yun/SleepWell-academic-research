package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.SleepRecord
import java.io.File
import java.time.Instant
import java.time.ZoneId

@Suppress("DEPRECATION")
class WearableSurveyRegressionTest : InstrumentationTestCase() {
    private val now = Instant.parse("2026-11-05T03:00:00Z").toEpochMilli()
    private val zone = ZoneId.of("Asia/Seoul")

    fun testSelectedWatchSleepAcceptsQuestionnaireWithoutCreatingManualDiary() {
        val parsed = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-05",
            sleepLatencyText = "20", awakeMinutesText = "15", rested = 5, condition = 2),
            nowMs = now, zoneId = zone, linkedSleep = watch("2026-11-05"))
        assertTrue(parsed.errors.joinToString(), parsed.ok)
        assertNull(parsed.diary)
        assertNull(parsed.input)
        assertEquals("2026-11-05", parsed.survey!!.sleepWakeDate)
        assertEquals(20, parsed.survey!!.sleepLatencyMinutes)
        assertEquals(15, parsed.survey!!.awakeMinutes)
        assertEquals(5, parsed.survey!!.rested)
    }

    fun testQuestionnaireRejectsWrongSleepFutureSleepAndImpossibleCombinedMeasurements() {
        val wrong = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-04", rested = 4),
            nowMs = now, zoneId = zone, linkedSleep = watch("2026-11-05"))
        assertFalse(wrong.ok)
        val future = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-06", rested = 4),
            nowMs = now, zoneId = zone, linkedSleep = watch("2026-11-06"))
        assertFalse(future.ok)
        val impossible = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-05", sleepLatencyText = "180"),
            nowMs = now, zoneId = zone, linkedSleep = watch("2026-11-05").copy(awakeMinutes = 300))
        assertFalse(impossible.ok)
        val ambiguous = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-05",
            bedText = "2026-11-04 23:00", wakeText = "2026-11-05 07:00"),
            nowMs = now, zoneId = zone, linkedSleep = watch("2026-11-05"))
        assertFalse(ambiguous.ok)
        assertNull(ambiguous.diary)
    }

    fun testTwoSleepQuestionnairesOnSameEntryDayPersistIndependentlyAndEditOnlySelectedSleep() {
        val file = File(instrumentation.targetContext.cacheDir, "wearable-surveys-${System.nanoTime()}.csv")
        try {
            SurveyStore.save(file, survey("2026-11-04", "10"), now)
            SurveyStore.save(file, survey("2026-11-05", "20"), now + 1)
            val loaded = SurveyStore.load(file)
            assertEquals(2, loaded.size)
            assertEquals(1, loaded.map { it.date }.distinct().size)
            assertEquals(setOf("2026-11-04", "2026-11-05"), loaded.map { it.sleepWakeDate }.toSet())
            SurveyStore.save(file, survey("2026-11-04", "25"), now + 2)
            val edited = SurveyStore.load(file)
            assertEquals(2, edited.size)
            assertEquals(25, edited.single { it.sleepWakeDate == "2026-11-04" }.sleepLatencyMinutes)
            assertEquals(20, edited.single { it.sleepWakeDate == "2026-11-05" }.sleepLatencyMinutes)
            val deleted = SurveyStore.delete(file, "me", edited.first().date, "2026-11-04")
            assertEquals(1, deleted.size)
            assertEquals("2026-11-05", deleted.single().sleepWakeDate)
        } finally { file.delete() }
    }

    fun testSelectedSleepKeepsCanonicalAssociationButDisplaysLocalWakeDate() {
        val parsed = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-05", rested = 4),
            nowMs = now, zoneId = ZoneId.of("America/New_York"), linkedSleep = watch("2026-11-05"))
        assertTrue(parsed.errors.joinToString(), parsed.ok)
        val roundTrip = SurveyCsv.parse(SurveyStore.toCsv(listOf(parsed.survey!!))).days.single()
        assertEquals("2026-11-05", roundTrip.sleepWakeDate)
        assertEquals(Instant.parse("2026-11-04T22:00:00Z").toEpochMilli(), roundTrip.sleepWakeMs)
        assertTrue(roundTrip.summaryIn(ZoneId.of("America/New_York")).contains("11/4 17:00"))
    }

    fun testSleepOnlyResponseDoesNotReplaceUpcomingAlarmContextAndNextDayEditIsNotDuplicate() {
        val file = File(instrumentation.targetContext.cacheDir, "wearable-context-${System.nanoTime()}.csv")
        try {
            val context = DirectInputParser.parse(DirectInputForm(targetWakeText = "07:15", caffeineMgText = "100"),
                nowMs = now, zoneId = zone).survey!!
            SurveyStore.saveInput(file, context, now)
            SurveyStore.saveInput(file, survey("2026-11-05", "20"), now + 1)
            val first = SurveyStore.load(file)
            assertEquals(2, first.size)
            val selected = SurveyObservationPolicy.currentNight(first, "me", Instant.ofEpochMilli(now + 2))!!.survey
            assertEquals(context.targetWakeMs, selected.targetWakeMs)
            assertEquals(100.0, selected.caffeineMg)
            assertNull(selected.sleepWakeDate)
            SurveyStore.saveInput(file, survey("2026-11-05", "25").copy(date = "2026-11-06"), now + 86_400_000)
            val edited = SurveyStore.load(file)
            assertEquals(2, edited.size)
            assertEquals(25, edited.single { it.sleepWakeDate != null }.sleepLatencyMinutes)
        } finally { file.delete() }
    }

    fun testCombinedInputSavesIndependentSleepAndUpcomingContextRows() {
        val file = File(instrumentation.targetContext.cacheDir, "wearable-combined-${System.nanoTime()}.csv")
        try {
            val parsed = DirectInputParser.parse(DirectInputForm(sleepWakeDate = "2026-11-05",
                rested = 5, condition = 2, sleepLatencyText = "15", targetWakeText = "07:00", napMinText = "20", napEndText = "10:00"),
                nowMs = now, zoneId = zone, linkedSleep = watch("2026-11-05"))
            assertTrue(parsed.errors.joinToString(), parsed.ok)
            val saved = SurveyStore.saveInput(file, parsed.survey!!, now)
            val sleep = saved.single { it.sleepWakeDate != null }
            val context = saved.single { it.sleepWakeDate == null }
            assertEquals(5, sleep.rested)
            assertNull(sleep.targetWakeMs)
            assertNull(sleep.napEndMs)
            assertNotNull(context.targetWakeMs)
            assertNotNull(context.napEndMs)
            assertNull(context.rested)
            assertNull(context.sleepLatencyMinutes)
        } finally { file.delete() }
    }

    fun testStrengthOnlyLinkedSleepSupplementSurvivesSaveInputAndReload() {
        val file = File(instrumentation.targetContext.cacheDir, "wearable-strength-only-${System.nanoTime()}.csv")
        try {
            val strengthOnly = survey("2026-11-05", "20").copy(
                rested = null,
                fatigue = null,
                sleepLatencyMinutes = null,
                awakeMinutes = null,
                exerciseMinutes = null,
                exerciseIntensity = "light"
            )
            val saved = SurveyStore.saveInput(file, strengthOnly, now)
            assertEquals(1, saved.size)
            val restored = SurveyStore.load(file).single()
            assertEquals("2026-11-05", restored.sleepWakeDate)
            assertEquals("light", restored.exerciseIntensity)
            assertNull(restored.exerciseMinutes)
        } finally { file.delete() }
    }

    private fun survey(day: String, latency: String) = DirectInputParser.parse(
        DirectInputForm(sleepWakeDate = day, sleepLatencyText = latency, rested = 4),
        nowMs = now, zoneId = zone, linkedSleep = watch(day)).survey!!

    private fun watch(day: String): SleepRecord {
        val wake = java.time.LocalDate.parse(day).atTime(7, 0).atZone(zone).toInstant()
        return SleepRecord("watch:$day", day, wake.minusSeconds(8 * 3600).toString(), wake.toString(),
            480, null, null, "health_connect", wake.plusSeconds(60).toString())
    }
}
