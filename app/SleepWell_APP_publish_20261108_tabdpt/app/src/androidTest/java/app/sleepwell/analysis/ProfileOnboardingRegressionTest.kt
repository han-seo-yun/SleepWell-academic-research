package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.UserModelEngine
import java.io.File
import java.time.Instant
import java.time.ZoneId

@Suppress("DEPRECATION")
class ProfileOnboardingRegressionTest : InstrumentationTestCase() {

    fun testMissingDefaultAndLegacyProfilesRequireOnboardingWithoutLosingValidValues() {
        assertTrue(ProfileStore.needsOnboarding(null))
        assertTrue(ProfileStore.needsOnboarding(NextNightModel.ModelProfile()))
        val file = tempFile("legacy")
        try {
            file.writeText("ageBand=30-44\nsex=female\nactivityMinutes=120\ndesiredSleepMinutes=540\nusualBed=22:15\nusualWake=07:15\n")
            val loaded = ProfileStore.load(file)!!
            assertEquals("30-44", loaded.ageBand)
            assertEquals("female", loaded.sex)
            assertEquals(120, loaded.activityMinutes)
            assertEquals(540, loaded.desiredSleepMinutes)
            assertEquals("22:15", loaded.usualBed)
            assertEquals("07:15", loaded.usualWake)
            assertEquals("", loaded.occupation)
            assertFalse(loaded.onboardingCompleted)
            assertTrue(ProfileStore.needsOnboarding(loaded))
        } finally {
            file.delete()
        }
    }

    fun testCompletedProfileRoundTripsWithoutOccupationAndLegacyValueIsPreserved() {
        val file = tempFile("roundtrip")
        try {
            val complete = NextNightModel.ModelProfile(
                ageBand = "45-59", sex = "any", occupation = "",
                activityMinutes = 120, desiredSleepMinutes = 510,
                usualBed = "23:15", usualWake = "07:45", onboardingCompleted = true
            )
            ProfileStore.save(file, complete)
            assertEquals(complete, ProfileStore.load(file))
            assertFalse(ProfileStore.needsOnboarding(ProfileStore.load(file)))

            val legacy = complete.copy(occupation = "legacy-free-text")
            ProfileStore.save(file, legacy)
            assertEquals("legacy-free-text", ProfileStore.load(file)!!.occupation)
            assertFalse(ProfileStore.needsOnboarding(ProfileStore.load(file)))
            val edited = legacy.copy(desiredSleepMinutes = 480, usualWake = "07:30")
            ProfileStore.save(file, edited)
            val editedReloaded = ProfileStore.load(file)!!
            assertEquals("legacy-free-text", editedReloaded.occupation)
            assertFalse(ProfileStore.summary(editedReloaded).contains("legacy-free-text"))

            val legacyDraft = complete.copy(ageBand = "legacy-value", onboardingCompleted = false)
            ProfileStore.save(file, legacyDraft)
            val reloaded = ProfileStore.load(file)!!
            assertFalse(reloaded.onboardingCompleted)
            assertTrue(ProfileStore.needsOnboarding(reloaded))
        } finally {
            file.delete()
        }
    }

    fun testCompletedSaveRequiresOnlyValidSexAndNeverInfersOrRejectsLegacyAge() {
        val file = tempFile("invalid")
        try {
            ProfileStore.save(file, NextNightModel.ModelProfile(occupation = "", onboardingCompleted = true))
            val withoutAge = ProfileStore.load(file)!!
            assertEquals("", withoutAge.ageBand)
            assertFalse(ProfileStore.needsOnboarding(withoutAge))

            val legacyAge = NextNightModel.ModelProfile(ageBand = "17", onboardingCompleted = true)
            ProfileStore.save(file, legacyAge)
            assertEquals("17", ProfileStore.load(file)!!.ageBand)
            assertFalse(ProfileStore.needsOnboarding(ProfileStore.load(file)))

            assertFails {
                ProfileStore.save(file, NextNightModel.ModelProfile(sex = "unspecified", onboardingCompleted = true))
            }

            file.writeText("ageBand=not-an-age\nsex=female\noccupation=student\nonboardingCompleted=true\n")
            val malformed = ProfileStore.load(file)!!
            assertEquals("not-an-age", malformed.ageBand)
            assertTrue(malformed.onboardingCompleted)
            assertFalse(ProfileStore.needsOnboarding(malformed))

            file.writeText("sex=female\nonboardingCompleted=true\n")
            val noAgeProperty = ProfileStore.load(file)!!
            assertEquals("", noAgeProperty.ageBand)
            assertTrue(noAgeProperty.onboardingCompleted)
            assertFalse(ProfileStore.needsOnboarding(noAgeProperty))
        } finally {
            file.delete()
        }
    }

    fun testAdapterAndRealEnginePreserveDeclaredProfileContractWithoutOccupationCoefficient() {
        val old = NextNightModel.profile
        try {
            NextNightModel.profile = NextNightModel.ModelProfile(
                ageBand = "30-44", sex = "male", occupation = "student",
                activityMinutes = 15, desiredSleepMinutes = 510,
                usualBed = "00:15", usualWake = "08:45", onboardingCompleted = true
            )
            val cutoff = Instant.parse("2026-11-05T11:00:00Z").atZone(ZoneId.of("Asia/Seoul"))
            val adapted = NextNightModel.userProfile(emptyList(), cutoff)
            assertEquals("student", adapted.occupation)
            assertEquals("30-44", adapted.ageBand)
            assertEquals("male", adapted.sex)
            assertEquals(15, adapted.activityMinutes)
            assertEquals(510, adapted.desiredSleepMinutes)
            assertEquals("00:15", adapted.usualBed)
            assertEquals("08:45", adapted.usualWake)

            val output = UserModelEngine(instrumentation.targetContext).compute(adapted, emptyList(), cutoff.toInstant())
            val contract = output.outcomes.getJSONObject("profile_contract")
            assertEquals("student", contract.getString("occupation"))
            assertTrue(contract.isNull("occupation_model_coefficient"))
            assertEquals(510, contract.getInt("desired_sleep_minutes"))
            val personalization = output.outcomes.getJSONObject("personalization")
            assertEquals(510.0, personalization.getDouble("baseline_duration_min"), 0.0)
            assertFalse(personalization.getJSONObject("population_prior").getBoolean("used_for_target"))

            val demographicVariant = adapted.copy(ageBand = "60-80", sex = "female", activityMinutes = 120)
            val demographicOutput = UserModelEngine(instrumentation.targetContext)
                .compute(demographicVariant, emptyList(), cutoff.toInstant())
            val originalPrior = personalization.getJSONObject("population_prior")
            val variantPrior = demographicOutput.outcomes.getJSONObject("personalization")
                .getJSONObject("population_prior")
            assertFalse(originalPrior.getBoolean("used_for_target"))
            assertFalse(variantPrior.getBoolean("used_for_target"))
            assertFalse(originalPrior.getString("level") == variantPrior.getString("level"))
            assertFalse(originalPrior.getDouble("mean_sleep_min") == variantPrior.getDouble("mean_sleep_min"))
            assertEquals(output.recommendation.durationMinutes, demographicOutput.recommendation.durationMinutes)
            assertEquals(output.recommendation.bedtime, demographicOutput.recommendation.bedtime)
            assertEquals(output.recommendation.wakeTime, demographicOutput.recommendation.wakeTime)

            val shortTarget = UserModelEngine(instrumentation.targetContext)
                .compute(adapted.copy(desiredSleepMinutes = 420), emptyList(), cutoff.toInstant())
            val longTarget = UserModelEngine(instrumentation.targetContext)
                .compute(adapted.copy(desiredSleepMinutes = 540), emptyList(), cutoff.toInstant())
            assertEquals(420.0, shortTarget.outcomes.getJSONObject("personalization").getDouble("baseline_duration_min"), 0.0)
            assertEquals(540.0, longTarget.outcomes.getJSONObject("personalization").getDouble("baseline_duration_min"), 0.0)
            // Recommendation is time in bed: declared sleep + 15 min latency + up to 30 min candidate margin.
            assertTrue(shortTarget.recommendation.durationMinutes in 435..465)
            assertTrue(longTarget.recommendation.durationMinutes in 555..585)
            assertTrue(longTarget.recommendation.durationMinutes > shortTarget.recommendation.durationMinutes)

            val worker = UserModelEngine(instrumentation.targetContext)
                .compute(adapted.copy(occupation = "worker"), emptyList(), cutoff.toInstant())
            assertEquals("worker", worker.outcomes.getJSONObject("profile_contract").getString("occupation"))
            assertEquals(output.recommendation.durationMinutes, worker.recommendation.durationMinutes)
            assertEquals(output.recommendation.bedtime, worker.recommendation.bedtime)
        } finally {
            NextNightModel.profile = old
        }
    }

    fun testLegacyOccupationValuesRemainAvailableForStorageAndBackupCompatibility() {
        assertEquals(
            listOf("학생" to "student", "직장인" to "worker", "기타" to "other"),
            ProfileStore.OCCUPATIONS
        )
    }

    fun testManualDesiredDurationAcceptsMinutePrecisionAndContractEndpoints() {
        assertEquals(420, ProfileStore.parseDesiredSleepMinutes("7", "0"))
        assertEquals(465, ProfileStore.parseDesiredSleepMinutes("7", "45"))
        assertEquals(487, ProfileStore.parseDesiredSleepMinutes(" 8 ", "07"))
        assertEquals(540, ProfileStore.parseDesiredSleepMinutes("9", "00"))
    }

    fun testManualDesiredDurationRejectsOutOfRangeAndMalformedFields() {
        listOf("6" to "59", "9" to "1", "7" to "60", "7" to "-1",
            "7.5" to "0", "+7" to "0", "" to "0", "7" to "", "9999999999" to "0").forEach { (hours, minutes) ->
            assertNull("$hours hours / $minutes minutes", ProfileStore.parseDesiredSleepMinutes(hours, minutes))
        }
    }

    fun testManuallyEnteredDurationSurvivesReloadAndReachesPersonalization() {
        val file = tempFile("manual-duration")
        val originalProfile = NextNightModel.profile
        try {
            val entered = NextNightModel.ModelProfile(onboardingCompleted = true,
                desiredSleepMinutes = ProfileStore.parseDesiredSleepMinutes("7", "45")!!)
            ProfileStore.save(file, entered)
            NextNightModel.profile = ProfileStore.load(file)!!
            assertEquals(465, NextNightModel.profile.desiredSleepMinutes)
            val cutoff = Instant.parse("2026-11-05T11:00:00Z").atZone(ZoneId.of("Asia/Seoul"))
            val adapted = NextNightModel.userProfile(emptyList(), cutoff)
            val output = UserModelEngine(instrumentation.targetContext).compute(adapted, emptyList(), cutoff.toInstant())
            assertEquals(465, output.outcomes.getJSONObject("profile_contract").getInt("desired_sleep_minutes"))
            assertEquals(465.0, output.outcomes.getJSONObject("personalization").getDouble("baseline_duration_min"), 0.0)
        } finally {
            NextNightModel.profile = originalProfile
            file.delete()
        }
    }

    private fun tempFile(suffix: String) =
        File(instrumentation.targetContext.cacheDir, "profile-onboarding-$suffix-${System.nanoTime()}.properties")

    private fun assertFails(block: () -> Unit) {
        try {
            block()
            fail("예외가 발생해야 합니다.")
        } catch (_: IllegalArgumentException) {
            // expected
        }
    }
}
