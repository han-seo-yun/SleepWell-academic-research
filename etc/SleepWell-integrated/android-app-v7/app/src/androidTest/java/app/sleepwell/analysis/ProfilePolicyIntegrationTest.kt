package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DisplayZone
import java.io.File
import java.time.Instant

@Suppress("DEPRECATION")
class ProfilePolicyIntegrationTest : InstrumentationTestCase() {
    fun testLegacyProfileMigrationAndNewPreferencesRoundTrip() {
        val file = File(instrumentation.targetContext.cacheDir, "profile-policy-${System.nanoTime()}.properties")
        try {
            file.writeText("ageBand=30-44\nsex=female\nactivityMinutes=120\n")
            val migrated = ProfileStore.load(file)!!
            assertEquals("30-44", migrated.ageBand)
            assertEquals(480, migrated.desiredSleepMinutes)
            assertEquals("23:30", migrated.usualBed)
            val changed = migrated.copy(desiredSleepMinutes = 540, usualBed = "22:15", usualWake = "07:15")
            ProfileStore.save(file, changed)
            assertEquals(changed, ProfileStore.load(file))
            assertNull(ProfileStore.validClock("24:30"))
            assertNull(ProfileStore.validClock("7:30"))
        } finally {
            file.delete()
        }
    }

    fun testDiaryOnlyPlanUsesDeclaredTargetAndCurrentTimeWithoutSensorHistory() {
        val oldProfile = NextNightModel.profile
        val oldDiary = NextNightModel.diaryRecords
        val oldDemo = NextNightModel.demoMode
        val oldZone = DisplaySettings.zone
        try {
            DisplaySettings.zone = DisplayZone.KST
            NextNightModel.install { instrumentation.targetContext.assets.open(it).use { stream -> stream.readBytes() } }
            NextNightModel.diaryRecords = emptyList()
            NextNightModel.demoMode = false
            val now = Instant.parse("2026-11-03T03:00:00Z").toEpochMilli()
            NextNightModel.profile = NextNightModel.ModelProfile(desiredSleepMinutes = 420)
            val short = AnalysisRunner.runDiary(null, emptyMap(), now)
            NextNightModel.profile = NextNightModel.ModelProfile(desiredSleepMinutes = 540)
            val long = AnalysisRunner.runDiary(null, emptyMap(), now)
            assertNull(NextNightModel.lastError)
            assertTrue(short.scHistory.isEmpty())
            assertTrue(long.sleepRecords.isEmpty())
            val shortPlan = short.recommendation!!
            val longPlan = long.recommendation!!
            assertTrue(shortPlan.recommendedSleepMin!! > 0)
            assertTrue(longPlan.recommendedSleepMin!! > shortPlan.recommendedSleepMin!!)
            assertEquals(now, longPlan.generatedAtMs)
            assertEquals("gradual_v1", longPlan.runtimePolicy)
            assertTrue(longPlan.personalizationStatus!!.contains("v5"))
            assertTrue(longPlan.recommendedBedTimeMs!! >= now)
            val display = AnalysisPresenter.build(long, recommendationReferenceMs = now)
            assertTrue(display.history.isEmpty())
            assertNotNull(display.schedule)
            assertTrue(display.readings.isEmpty())
            val saved = ResultStore.fromDisplay(display, "직접 수면일지", null, now)!!
            assertEquals("", saved.sValue)
            assertEquals("", saved.cValue)
        } finally {
            NextNightModel.profile = oldProfile
            NextNightModel.diaryRecords = oldDiary
            NextNightModel.demoMode = oldDemo
            DisplaySettings.zone = oldZone
        }
    }
}
