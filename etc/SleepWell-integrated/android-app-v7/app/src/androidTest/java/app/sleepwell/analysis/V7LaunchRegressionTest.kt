package app.sleepwell.analysis

import android.app.Activity
import android.content.Intent
import android.test.InstrumentationTestCase
import app.sleepwell.MainActivity
import java.io.File

/** Launch/render smoke tests. No sleep fixtures are written to the user's files directory. */
@Suppress("DEPRECATION")
class V7LaunchRegressionTest : InstrumentationTestCase() {
    fun testFirstRunRendersWithoutAnySleepFixture() = launchWithProfile(false)

    fun testDashboardRendersWithoutCreatingDemoRecords() = launchWithProfile(true)

    private fun launchWithProfile(completed: Boolean) {
        val context = instrumentation.targetContext
        val profileFile = File(context.filesDir, ProfileStore.FILE_NAME)
        val originalProfile = profileFile.takeIf { it.exists() }?.readBytes()
        val protectedFiles = listOf("survey_log.csv", "saved_results.csv", DiaryStore.FILE_NAME)
            .associateWith { name -> File(context.filesDir, name).takeIf { it.exists() }?.readBytes() }
        var activity: Activity? = null
        try {
            // Temporary UI-test profile only; the exact previous profile is restored in finally.
            ProfileStore.save(profileFile, NextNightModel.ModelProfile(
                occupation = "other", onboardingCompleted = completed))
            activity = instrumentation.startActivitySync(Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            instrumentation.waitForIdleSync()
            assertNotNull(activity)
            assertFalse(activity.isFinishing)
            assertFalse(activity.isDestroyed)
            instrumentation.runOnMainSync { assertTrue(activity.window.decorView.isShown) }
            assertFalse("User launch must never select a demo model", NextNightModel.demoMode)
            protectedFiles.forEach { (name, original) ->
                val current = File(context.filesDir, name).takeIf { it.exists() }?.readBytes()
                assertTrue("Launching must preserve $name", if (original == null) current == null
                    else current != null && original.contentEquals(current))
            }
        } finally {
            activity?.let { opened -> instrumentation.runOnMainSync { opened.finish() } }
            instrumentation.waitForIdleSync()
            if (originalProfile == null) profileFile.delete() else profileFile.writeBytes(originalProfile)
        }
    }
}
