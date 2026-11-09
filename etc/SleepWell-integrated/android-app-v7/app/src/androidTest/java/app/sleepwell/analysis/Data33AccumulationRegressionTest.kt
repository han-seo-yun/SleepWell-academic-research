package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.EngineOutput
import app.sleepwell.user.SleepRecord
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import java.io.File
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class Data33AccumulationRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")
    private val decisionTime = ZonedDateTime.of(2026, 11, 10, 20, 0, 0, 0, seoul).toInstant()

    fun testDistinctDayDiarySaveReloadGrowsRealModelHistoryAndChangesFeatures() {
        val dir = isolatedDir("diary-growth")
        val file = File(dir, DiaryStore.FILE_NAME)
        try {
            DiaryStore.save(file, record("2026-11-07", 0, 0, 7, 0, 420, "2026-11-07T00:00:00Z"))
            val firstReload = DiaryStore.load(file)
            val first = compute(firstReload)
            val firstFeatures = first.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
            assertEquals(1.0, firstFeatures.getDouble("history_count_7"), 0.0)
            assertEquals(1, first.outcomes.getJSONObject("research_personalization").getJSONObject("cmi").getInt("valid_days"))
            assertEquals(1, first.outcomes.getJSONObject("personalization").getInt("eligible_primary_nights"))

            DiaryStore.save(file, record("2026-11-08", 22, 0, 7, 0, 540, "2026-11-08T00:00:00Z"))
            val secondReload = DiaryStore.load(file)
            val second = compute(secondReload)
            val secondFeatures = second.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")

            assertEquals(2, secondReload.size)
            assertEquals(2.0, secondFeatures.getDouble("history_count_7"), 0.0)
            assertEquals(2, second.outcomes.getJSONObject("research_personalization").getJSONObject("cmi").getInt("valid_days"))
            assertEquals(2, second.outcomes.getJSONObject("personalization").getInt("eligible_primary_nights"))
            assertFalse(firstFeatures.getDouble("previous_duration") == secondFeatures.getDouble("previous_duration"))
            assertFalse(firstFeatures.getDouble("habit_bed") == secondFeatures.getDouble("habit_bed"))
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testSameWakeDayCorrectionReplacesRowWithoutInflatingModelHistory() {
        val dir = isolatedDir("same-day-correction")
        val file = File(dir, DiaryStore.FILE_NAME)
        try {
            DiaryStore.save(file, record("2026-11-07", 23, 0, 7, 0, 480, "2026-11-07T00:00:00Z"))
            val original = record("2026-11-08", 22, 0, 7, 0, 540, "2026-11-08T00:00:00Z")
            DiaryStore.save(file, original)
            val before = compute(DiaryStore.load(file))
            val beforeFeatures = before.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")

            val corrected = record("2026-11-08", 23, 0, 7, 0, 480, "2026-11-08T01:00:00Z")
            val afterSave = DiaryStore.save(file, corrected)
            val reloaded = DiaryStore.load(file)
            val after = compute(reloaded)
            val afterFeatures = after.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")

            assertEquals(2, afterSave.size)
            assertEquals(2, reloaded.size)
            assertEquals(1, reloaded.count { it.id == "diary:2026-11-08" })
            assertEquals(corrected, reloaded.first { it.id == corrected.id })
            assertEquals(2.0, beforeFeatures.getDouble("history_count_7"), 0.0)
            assertEquals(2.0, afterFeatures.getDouble("history_count_7"), 0.0)
            assertEquals(2, after.outcomes.getJSONObject("personalization").getInt("eligible_primary_nights"))
            assertFalse(beforeFeatures.getDouble("previous_duration") == afterFeatures.getDouble("previous_duration"))
        } finally {
            dir.deleteRecursively()
        }
    }

    fun testRepeatedHealthConnectImportsRetainSessionOlderThanThirtyDays() {
        val dir = isolatedDir("health-connect-retention")
        try {
            val oldStart = ZonedDateTime.of(2026, 8, 1, 23, 0, 0, 0, seoul).toInstant()
            val oldEnd = oldStart.plus(Duration.ofHours(8))
            val recentStart = ZonedDateTime.of(2026, 9, 11, 23, 30, 0, 0, seoul).toInstant()
            val recentEnd = recentStart.plus(Duration.ofHours(7))
            assertTrue(Duration.between(oldEnd, recentStart).toDays() > 30)

            SleepDataStore.saveHealthConnect(dir, HealthConnectCsv.toCsv(listOf(
                HcSleepSession("old", oldStart.toEpochMilli(), oldEnd.toEpochMilli(), 9 * 3600, emptyList())
            )))
            SleepDataStore.saveHealthConnect(dir, HealthConnectCsv.toCsv(listOf(
                HcSleepSession("recent", recentStart.toEpochMilli(), recentEnd.toEpochMilli(), 9 * 3600, emptyList())
            )))

            val (_, info) = SleepDataStore.buildMerged(dir)!!
            assertEquals(2, info.healthConnectSessions)
            assertEquals(2, info.healthConnectAdded)
            assertEquals(2, info.total)
            assertEquals(oldStart.toEpochMilli(), info.firstMs ?: -1L)
            assertEquals(recentEnd.toEpochMilli(), info.lastMs ?: -1L)
        } finally {
            dir.deleteRecursively()
        }
    }

    private fun compute(records: List<SleepRecord>): EngineOutput {
        val profile = UserProfile(
            ageBand = "30-44", sex = "any", activityMinutes = 60,
            usualBed = "23:30", usualWake = "07:30", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z"
        )
        return UserModelEngine(instrumentation.targetContext).compute(profile, records, decisionTime)
    }

    private fun record(
        wakeDay: String,
        bedHour: Int,
        bedMinute: Int,
        wakeHour: Int,
        wakeMinute: Int,
        timeInBedMinutes: Int,
        modifiedAt: String
    ): SleepRecord {
        val wakeDate = java.time.LocalDate.parse(wakeDay)
        val wake = ZonedDateTime.of(wakeDate, java.time.LocalTime.of(wakeHour, wakeMinute), seoul).toInstant()
        val bed = wake.minus(Duration.ofMinutes(timeInBedMinutes.toLong()))
        return SleepRecord(
            id = "diary:$wakeDay", day = wakeDay, bedtime = bed.toString(), wakeTime = wake.toString(),
            durationMinutes = timeInBedMinutes, rested = 4, fatigue = 2, source = "manual",
            modifiedAt = Instant.parse(modifiedAt).toString(), sleepLatencyMinutes = 15, awakeMinutes = 15
        ).also {
            assertEquals(bedHour, bed.atZone(seoul).hour)
            assertEquals(bedMinute, bed.atZone(seoul).minute)
        }
    }

    private fun isolatedDir(label: String): File =
        File(instrumentation.targetContext.cacheDir, "data33-$label-${System.nanoTime()}").also { assertTrue(it.mkdirs()) }
}
