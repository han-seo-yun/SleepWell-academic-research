package app.sleepwell.analysis

import android.app.job.JobScheduler
import android.content.ComponentName
import android.content.pm.PackageManager
import android.test.InstrumentationTestCase
import app.sleepwell.user.SleepInputPolicy
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.io.IOException
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class BackgroundSleepSyncRegressionTest : InstrumentationTestCase() {
    private val now = ZonedDateTime.of(2026, 11, 5, 15, 0, 0, 0, ZoneId.of("Asia/Seoul"))
    private val nowMs get() = now.toInstant().toEpochMilli()

    fun testClosedAppImportReloadFeedsData33AndPersonalizationWithoutDiary() = runBlocking {
        val dir = isolatedDir("model-reload")
        try {
            val result = BackgroundSleepSync.runOnce(dir, { BackgroundSleepSync.Access.READY }, { imported(7) }, { nowMs })
            assertEquals(BackgroundSleepSync.Outcome.SAVED, result.outcome)
            assertEquals(7, result.sessionCount)
            // The next launch constructs a fresh engine and reloads the same disk store used by AnalysisScreen.
            val nights = ImportedSleepStore.loadModelNights(dir, nowMs + 60_000)
            assertEquals(7, nights.size)
            assertTrue(nights.all { it.source == "samsung_health" })
            assertTrue(nights.all { it.modifiedAt == Instant.ofEpochMilli(nowMs).toString() })
            val engine = UserModelEngine(instrumentation.targetContext)
            val output = engine.compute(UserProfile(ageBand = "20-29", sex = "any", activityMinutes = 60,
                usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
                createdAt = "2026-09-01T00:00:00Z", averageSteps = 6_000.0),
                nights, now.plusMinutes(1).toInstant(), inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1)
            assertEquals(7, output.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
            assertEquals(7, output.outcomes.getJSONObject("personalization").getInt("eligible_input_nights"))
            val features = output.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
            // The persisted diagnostic snapshot includes DATA33 plus six physical features.
            assertEquals(39, features.length())
            assertEquals(7.0, features.getDouble("recent_count14"), 0.0)
        } finally { dir.deleteRecursively() }
    }

    fun testRepeatedBackgroundImportKeepsHistoryAndFirstObservation() = runBlocking {
        val dir = isolatedDir("repeat")
        try {
            val ready = suspend { BackgroundSleepSync.Access.READY }
            BackgroundSleepSync.runOnce(dir, ready, { imported(1, daysAgo = 45) }, { nowMs - 40 * 86_400_000L })
            BackgroundSleepSync.runOnce(dir, ready, { imported(3) }, { nowMs })
            val original = File(dir, ImportedSleepStore.FILE_NAME).readBytes()
            BackgroundSleepSync.runOnce(dir, ready, { imported(3) }, { nowMs + 60_000 })
            assertTrue(original.contentEquals(File(dir, ImportedSleepStore.FILE_NAME).readBytes()))
            assertEquals(4, ImportedSleepStore.loadModelNights(dir, nowMs + 120_000).size)
        } finally { dir.deleteRecursively() }
    }

    fun testMissingPermissionOrUnsupportedDeviceNeverCallsReader() = runBlocking {
        val dir = isolatedDir("gates")
        try {
            var reads = 0
            BackgroundSleepSync.Access.entries.filter { it != BackgroundSleepSync.Access.READY }.forEach { access ->
                val result = BackgroundSleepSync.runOnce(dir, { access }, { reads++; imported(1) })
                assertEquals(if (access == BackgroundSleepSync.Access.CHECK_FAILED) BackgroundSleepSync.Outcome.RETRY
                    else BackgroundSleepSync.Outcome.BLOCKED, result.outcome)
            }
            assertEquals(0, reads)
            assertFalse(SleepDataStore.hasAny(dir))
        } finally { dir.deleteRecursively() }
    }

    fun testPermissionRevokedDuringReadDoesNotPersistResult() = runBlocking {
        val dir = isolatedDir("revoke")
        try {
            var checks = 0
            val result = BackgroundSleepSync.runOnce(dir,
                { if (++checks == 1) BackgroundSleepSync.Access.READY else BackgroundSleepSync.Access.BACKGROUND_PERMISSION_REQUIRED },
                { imported(1) }, { nowMs })
            assertEquals(BackgroundSleepSync.Outcome.BLOCKED, result.outcome)
            assertFalse(File(dir, ImportedSleepStore.FILE_NAME).exists())
        } finally { dir.deleteRecursively() }
    }

    fun testTransientAndSecurityFailuresKeepPreviouslySavedNights() = runBlocking {
        val dir = isolatedDir("failure")
        try {
            val ready = suspend { BackgroundSleepSync.Access.READY }
            BackgroundSleepSync.runOnce(dir, ready, { imported(1) }, { nowMs })
            val original = File(dir, ImportedSleepStore.FILE_NAME).readBytes()
            assertEquals(BackgroundSleepSync.Outcome.RETRY,
                BackgroundSleepSync.runOnce(dir, ready, { throw IOException("Provider temporarily unavailable") }).outcome)
            assertEquals(BackgroundSleepSync.Outcome.BLOCKED,
                BackgroundSleepSync.runOnce(dir, ready, { throw SecurityException("Revoked") }).outcome)
            assertTrue(original.contentEquals(File(dir, ImportedSleepStore.FILE_NAME).readBytes()))
        } finally { dir.deleteRecursively() }
    }

    fun testEmptyReadDoesNotClearHistory() = runBlocking {
        val dir = isolatedDir("empty")
        try {
            val ready = suspend { BackgroundSleepSync.Access.READY }
            BackgroundSleepSync.runOnce(dir, ready, { imported(2) }, { nowMs })
            val result = BackgroundSleepSync.runOnce(dir, ready,
                { HealthConnectSleep.ImportResult("", 0, false, null, null) }, { nowMs + 60_000 })
            assertEquals(BackgroundSleepSync.Outcome.EMPTY, result.outcome)
            assertEquals(2, ImportedSleepStore.loadModelNights(dir, nowMs + 60_000).size)
        } finally { dir.deleteRecursively() }
    }

    fun testStoppedOrTimedOutReadDoesNotSaveOrConvertCancellationToRetry() = runBlocking {
        val dir = isolatedDir("cancel")
        try {
            try {
                BackgroundSleepSync.runOnce(dir, { BackgroundSleepSync.Access.READY }, { throw CancellationException("Stopped") })
                fail("Cancellation must propagate to JobService")
            } catch (_: CancellationException) { }
            val result = withTimeoutOrNull(100) {
                BackgroundSleepSync.runOnce(dir, { BackgroundSleepSync.Access.READY }, { delay(5_000); imported(1) })
            }
            assertNull(result)
            assertFalse(SleepDataStore.hasAny(dir))
        } finally { dir.deleteRecursively() }
    }

    fun testSystemAcceptsOnePersistedPeriodicJobAndManifestProtectsService() {
        val context = instrumentation.targetContext
        val scheduler = context.getSystemService(JobScheduler::class.java)
        val testId = 53099
        assertNull(scheduler.getPendingJob(testId))
        try {
            val job = BackgroundSleepSync.buildJob(context, testId)
            assertEquals(JobScheduler.RESULT_SUCCESS, scheduler.schedule(job))
            assertEquals(JobScheduler.RESULT_SUCCESS, scheduler.schedule(job))
            assertEquals(1, scheduler.allPendingJobs.count { it.id == testId })
            val pending = scheduler.getPendingJob(testId)!!
            assertTrue(pending.isPersisted)
            assertTrue(pending.isPeriodic)
            assertEquals(BackgroundSleepSync.INTERVAL_MS, pending.intervalMillis)
            val service = context.packageManager.getServiceInfo(ComponentName(context, SleepSyncJobService::class.java), 0)
            assertEquals("android.permission.BIND_JOB_SERVICE", service.permission)
            assertEquals(context.applicationInfo.processName, service.processName)
            val permissions = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty().toSet()
            assertTrue(permissions.containsAll(HealthConnectSleep.BACKGROUND_PERMISSIONS))
            assertTrue(permissions.contains("android.permission.RECEIVE_BOOT_COMPLETED"))
        } finally { scheduler.cancel(testId) }
    }

    private fun isolatedDir(name: String) = File(instrumentation.targetContext.cacheDir,
        "background-sync-$name-${System.nanoTime()}").apply { mkdirs() }

    private fun imported(count: Int, daysAgo: Int = 0): HealthConnectSleep.ImportResult {
        val sessions = (0 until count).map { index ->
            val wake = now.minusDays((daysAgo + index).toLong()).withHour(7)
            HcSleepSession("background-${wake.toLocalDate()}", wake.minusHours(8).toInstant().toEpochMilli(),
                wake.toInstant().toEpochMilli(), 9 * 3600, emptyList(), source = "samsung_health")
        }
        return HealthConnectSleep.ImportResult(HealthConnectCsv.toCsv(sessions, idPrefix = ""), count, true,
            sessions.minOfOrNull { it.startMs }, sessions.maxOfOrNull { it.endMs })
    }
}
