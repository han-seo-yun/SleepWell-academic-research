package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import java.io.File
import java.time.Instant
import java.time.ZoneId
import app.sleepwell.user.SleepRecord

@Suppress("DEPRECATION")
class SurveyFormRestoreRegressionTest : InstrumentationTestCase() {
    private val now = Instant.parse("2026-11-06T12:00:00Z")

    fun testRestoreIsReadOnlyAndKeepsLegacyFatigueScale() {
        val file = tempFile("read-only")
        try {
            val legacy = context(fatigue = 4, scale = 7)
            file.writeText(SurveyStore.toCsv(listOf(legacy)))
            val before = file.readBytes()

            val restored = SurveyFormRestore.restore(SurveyStore.load(file), "me", now, ZoneId.of("Asia/Seoul"))

            assertEquals(4, restored.form.condition)
            assertEquals(7, restored.form.fatigueScaleMax)
            assertTrue(before.contentEquals(file.readBytes()))
        } finally { file.delete() }
    }

    fun testFivePointResponseRemainsFivePoint() {
        val current = context(fatigue = 5, scale = 5)
        val restored = SurveyFormRestore.restore(listOf(current), "me", now, ZoneId.of("Asia/Seoul"))
        assertEquals(5, restored.form.condition)
        assertEquals(5, restored.form.fatigueScaleMax)
    }

    fun testSelectedSleepWithoutSupplementDoesNotBorrowContextResponse() {
        val current = context(fatigue = 5, scale = 5)
        val restored = SurveyFormRestore.restore(
            listOf(current), "me", now, ZoneId.of("Asia/Seoul"), sleepWakeDate = "2026-11-05"
        )
        assertNull(restored.form.condition)
        assertNull(restored.form.rested)
        assertEquals(5, restored.form.fatigueScaleMax)
        assertEquals("2026-11-05", restored.form.sleepWakeDate)
    }

    fun testRawInstantsAreShownInCurrentDeviceZone() {
        val day = context(fatigue = null, scale = 5).copy(
            lastCaffeine = "19:00", lastCaffeineMs = Instant.parse("2026-11-06T10:00:00Z").toEpochMilli(),
            napEnd = "21:00", napEndMs = Instant.parse("2026-11-06T11:30:00Z").toEpochMilli(),
            alarmTime = "07:00", targetWakeMs = Instant.parse("2026-11-06T13:00:00Z").toEpochMilli()
        )
        val restored = SurveyFormRestore.restore(listOf(day), "me", now, ZoneId.of("America/New_York"))
        assertEquals("05:00", restored.form.lastCaffeineText)
        assertEquals("06:30", restored.form.napEndText)
        assertEquals("2026-11-06 08:00", restored.form.targetWakeText)
    }

    fun testExpiredExplicitTargetIsNotRestoredAsInvalidInput() {
        val expired = context(fatigue = 3, scale = 5).copy(
            targetWakeMs = now.minusSeconds(3600).toEpochMilli(), alarmTime = "20:00"
        )
        val restored = SurveyFormRestore.restore(listOf(expired), "me", now, ZoneId.of("Asia/Seoul"))
        assertEquals("", restored.form.targetWakeText)

        val file = tempFile("expired-target")
        try {
            file.writeText(SurveyStore.toCsv(listOf(expired)))
            val fatigueOnly = expired.copy(fatigue = 4, targetWakeMs = null, alarmTime = null)
            val saved = SurveyStore.saveRestoredEdit(file, fatigueOnly, expired, null, now.toEpochMilli()).single()
            assertEquals(expired.targetWakeMs, saved.targetWakeMs)
            assertEquals(expired.alarmTime, saved.alarmTime)
            assertEquals(4, saved.fatigue)
        } finally { file.delete() }
    }

    fun testEditingContextDoesNotRestampUnchangedSleepSupplement() {
        val file = tempFile("parts")
        try {
            val oldContext = context(fatigue = null, scale = 5).copy(caffeineMg = 100.0)
            val oldSleep = sleep(fatigue = 4, scale = 7)
            file.writeText(SurveyStore.toCsv(listOf(oldContext, oldSleep)))
            val edited = oldSleep.copy(
                date = oldContext.date,
                caffeineUsed = true,
                caffeineMg = 150.0,
                lastCaffeine = null,
                alarmTime = null,
                targetWakeMs = null
            )

            val saved = SurveyStore.saveRestoredEdit(file, edited, oldContext, oldSleep, now.toEpochMilli())
            val context = saved.single { it.sleepWakeDate == null }
            val supplement = saved.single { it.sleepWakeDate != null }
            assertEquals(150.0, context.caffeineMg)
            assertEquals(now, Instant.parse(context.savedAt))
            assertEquals(oldSleep.savedAt, supplement.savedAt)
            assertEquals(4, supplement.fatigue)
            assertEquals(7, supplement.fatigueScaleMax)
        } finally { file.delete() }
    }

    fun testUnchangedEditPreservesBytesAndObservationTime() {
        val file = tempFile("unchanged")
        try {
            val old = context(fatigue = 3, scale = 5)
            file.writeText(SurveyStore.toCsv(listOf(old)))
            val before = file.readBytes()
            val saved = SurveyStore.saveRestoredEdit(file, old, old, null, now.toEpochMilli())
            assertEquals(old.savedAt, saved.single().savedAt)
            assertTrue(before.contentEquals(file.readBytes()))
        } finally { file.delete() }
    }

    fun testMissingRestoreBaselineFallsBackToStoredRowForHiddenFields() {
        val file = tempFile("baseline-fallback")
        try {
            val old = context(fatigue = 3, scale = 5)
            file.writeText(SurveyStore.toCsv(listOf(old)))
            val saved = SurveyStore.saveRestoredEdit(
                file, old.copy(caffeineMg = 140.0, heartRate = null, steps = null), null, null, now.toEpochMilli()
            ).single()
            assertEquals(62.0, saved.heartRate)
            assertEquals(3210, saved.steps)
            assertEquals(140.0, saved.caffeineMg)
        } finally { file.delete() }
    }

    fun testDirtyFormCrossingNightBoundaryKeepsOldHistoryAndAddsNewNight() {
        val file = tempFile("rollover")
        try {
            val old = context(fatigue = null, scale = 5).copy(date = "2026-11-05")
            file.writeText(SurveyStore.toCsv(listOf(old)))
            val nextNight = old.copy(date = "2026-11-06", caffeineMg = 120.0, savedAt = null)
            val saved = SurveyStore.saveRestoredEdit(file, nextNight, old, null, now.toEpochMilli())
            assertEquals(setOf("2026-11-05", "2026-11-06"), saved.map { it.date }.toSet())
            assertEquals(old.savedAt, saved.single { it.date == "2026-11-05" }.savedAt)
            assertEquals(120.0, saved.single { it.date == "2026-11-06" }.caffeineMg)
        } finally { file.delete() }
    }

    fun testEmptyDraftAfterNightRolloverDoesNotDeletePreviousNight() {
        val file = tempFile("empty-rollover")
        try {
            val old = context(fatigue = null, scale = 5).copy(date = "2026-11-05")
            file.writeText(SurveyStore.toCsv(listOf(old)))
            val saved = SurveyStore.saveRestoredEdit(
                file, null, old, null, now.toEpochMilli(), emptyEditContextDate = "2026-11-06"
            )
            assertEquals(1, saved.size)
            assertEquals("2026-11-05", saved.single().date)
            assertEquals(old.savedAt, saved.single().savedAt)
        } finally { file.delete() }
    }

    fun testParserRoundTripDoesNotRestampOtherSleepMeasurement() {
        val file = tempFile("parser-roundtrip")
        try {
            val oldContext = context(fatigue = null, scale = 5).copy(
                targetWakeMs = Instant.parse("2026-11-06T22:00:00Z").toEpochMilli(),
                lastCaffeineMs = Instant.parse("2026-11-06T09:00:00Z").toEpochMilli(),
                napEndMs = Instant.parse("2026-11-06T06:00:00Z").toEpochMilli(),
                exerciseMinutes = null, exerciseIntensity = null, rested = null
            )
            val oldSleep = sleep(fatigue = 4, scale = 7)
            file.writeText(SurveyStore.toCsv(listOf(oldContext, oldSleep)))
            val restored = SurveyFormRestore.restore(
                SurveyStore.load(file), "me", now, ZoneId.of("Asia/Seoul"), oldSleep.sleepWakeDate
            )
            val linked = SleepRecord(
                id = "watch", day = oldSleep.sleepWakeDate!!,
                bedtime = "2026-11-05T16:00:00Z", wakeTime = "2026-11-06T00:00:00Z",
                durationMinutes = 480, rested = null, fatigue = null, source = "wearable",
                modifiedAt = "2026-11-06T00:01:00Z"
            )
            val parsed = DirectInputParser.parse(
                restored.form, nowMs = now.toEpochMilli(), zoneId = ZoneId.of("Asia/Seoul"), linkedSleep = linked
            )
            assertTrue(parsed.errors.joinToString(), parsed.ok)
            val saved = SurveyStore.saveRestoredEdit(
                file, parsed.survey, restored.context, restored.sleepSupplement, now.toEpochMilli()
            )
            val context = saved.single { it.sleepWakeDate == null }
            val sleep = saved.single { it.sleepWakeDate != null }
            assertEquals(oldContext.savedAt, context.savedAt)
            assertEquals(oldContext.lastCaffeineMs, context.lastCaffeineMs)
            assertEquals(oldContext.napEndMs, context.napEndMs)
            assertEquals(oldContext.targetWakeMs, context.targetWakeMs)
            assertEquals(oldSleep.savedAt, sleep.savedAt)
            assertEquals(oldSleep.fatigue, sleep.fatigue)
            assertEquals(oldSleep.fatigueScaleMax, sleep.fatigueScaleMax)
        } finally { file.delete() }
    }

    private fun context(fatigue: Int?, scale: Int) = SurveyDay(
        participant = "me", date = "2026-11-06", caffeineUsed = true, caffeineMg = 80.0,
        lastCaffeine = "18:00", napMinutes = 20.0, napEnd = "15:00", exerciseMinutes = 30.0,
        exerciseEnd = null, exerciseIntensity = "moderate", fatigue = fatigue, steps = 3210,
        heartRate = 62.0, alarmTime = "07:00", firstCommitment = "09:00", examTomorrow = false,
        alcohol = false, illness = false, savedAt = "2026-11-06T11:00:00Z", rested = 4,
        sourceZoneId = "Asia/Seoul", sourceDate = "2026-11-06", fatigueScaleMax = scale
    )

    private fun sleep(fatigue: Int, scale: Int) = context(fatigue, scale).copy(
        caffeineUsed = null, caffeineMg = null, lastCaffeine = null, napMinutes = null, napEnd = null,
        alarmTime = null, firstCommitment = null, exerciseMinutes = 30.0,
        sleepWakeDate = "2026-11-06", sleepLatencyMinutes = 12, awakeMinutes = 18,
        sleepWakeMs = Instant.parse("2026-11-06T00:00:00Z").toEpochMilli(),
        savedAt = "2026-11-06T10:00:00Z"
    )

    private fun tempFile(label: String) =
        File(instrumentation.targetContext.cacheDir, "survey-restore-$label-${System.nanoTime()}.csv")
}
