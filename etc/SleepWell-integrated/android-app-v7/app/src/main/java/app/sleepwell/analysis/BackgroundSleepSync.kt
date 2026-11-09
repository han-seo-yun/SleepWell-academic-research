package app.sleepwell.analysis

import android.app.job.JobInfo
import android.app.job.JobScheduler
import android.content.ComponentName
import android.content.Context
import androidx.core.content.edit
import androidx.health.connect.client.HealthConnectClient
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.File

/** Same local import path as the foreground; never loads or mutates the UI's model singleton. */
object BackgroundSleepSync {
    const val JOB_ID = 53001
    const val INTERVAL_MS = 60 * 60_000L
    private const val PREFS = "background_sleep_sync"

    enum class Access { READY, PROVIDER_UNAVAILABLE, UNSUPPORTED, SLEEP_PERMISSION_REQUIRED, BACKGROUND_PERMISSION_REQUIRED, CHECK_FAILED }
    enum class Outcome { SAVED, EMPTY, BLOCKED, RETRY }
    data class SyncResult(val outcome: Outcome, val sessionCount: Int = 0)
    data class State(val access: Access, val scheduled: Boolean, val lastSuccessMs: Long, val lastOutcome: String?)

    suspend fun access(context: Context): Access = withContext(Dispatchers.IO) {
        try {
            if (HealthConnectSleep.availability(context) != HealthConnectSleep.Availability.AVAILABLE) {
                Access.PROVIDER_UNAVAILABLE
            } else if (!HealthConnectSleep.supportsBackgroundRead(context)) {
                Access.UNSUPPORTED
            } else {
                val granted = HealthConnectClient.getOrCreate(context).permissionController.getGrantedPermissions()
                when {
                    !granted.containsAll(HealthConnectSleep.PERMISSIONS) -> Access.SLEEP_PERMISSION_REQUIRED
                    !granted.containsAll(HealthConnectSleep.BACKGROUND_PERMISSIONS) -> Access.BACKGROUND_PERMISSION_REQUIRED
                    else -> Access.READY
                }
            }
        } catch (e: CancellationException) { throw e }
        catch (_: Exception) { Access.CHECK_FAILED }
    }

    /** Call on foreground resume/permission result. Permission dialogs are never launched by a job. */
    suspend fun refresh(context: Context): State {
        val app = context.applicationContext
        val access = access(app)
        val scheduler = app.getSystemService(JobScheduler::class.java)
        when (access) {
            Access.READY -> if (scheduler.getPendingJob(JOB_ID) == null) scheduler.schedule(buildJob(app))
            Access.CHECK_FAILED -> Unit // Keep a previously registered job across a transient provider error.
            else -> scheduler.cancel(JOB_ID)
        }
        val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return State(access, scheduler.getPendingJob(JOB_ID) != null,
            prefs.getLong("last_success_ms", 0L), prefs.getString("last_outcome", null))
    }

    internal fun buildJob(context: Context, id: Int = JOB_ID): JobInfo =
        JobInfo.Builder(id, ComponentName(context, SleepSyncJobService::class.java))
            .setPeriodic(INTERVAL_MS, 15 * 60_000L)
            .setPersisted(true)
            .setBackoffCriteria(5 * 60_000L, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
            .build()

    internal suspend fun runOnce(
        dataDir: File,
        access: suspend () -> Access,
        read: suspend () -> HealthConnectSleep.ImportResult,
        nowMs: () -> Long = System::currentTimeMillis
    ): SyncResult = withContext(Dispatchers.IO) {
        try {
            when (access()) {
                Access.READY -> Unit
                Access.CHECK_FAILED -> return@withContext SyncResult(Outcome.RETRY)
                else -> return@withContext SyncResult(Outcome.BLOCKED)
            }
            val imported = read()
            currentCoroutineContext().ensureActive()
            if (imported.sessionCount == 0) return@withContext SyncResult(Outcome.EMPTY)
            // A permission can be revoked while the provider read is in flight.
            when (access()) {
                Access.READY -> Unit
                Access.CHECK_FAILED -> return@withContext SyncResult(Outcome.RETRY)
                else -> return@withContext SyncResult(Outcome.BLOCKED)
            }
            currentCoroutineContext().ensureActive()
            SleepDataStore.saveHealthConnect(dataDir, imported.csv,
                source = if (imported.fromSamsung) "samsung_health" else "health_connect", observedAtMs = nowMs())
            SyncResult(Outcome.SAVED, imported.sessionCount)
        } catch (e: CancellationException) { throw e }
        catch (_: SecurityException) { SyncResult(Outcome.BLOCKED) }
        catch (_: Exception) { SyncResult(Outcome.RETRY) }
    }

    internal suspend fun run(context: Context): SyncResult = runOnce(
        File(context.filesDir, "sleep_data"),
        access = { access(context) }, read = { HealthConnectSleep.readAsCsv(context) }
    )

    internal fun recordResult(context: Context, result: SyncResult) {
        val now = System.currentTimeMillis()
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit {
            putLong("last_attempt_ms", now)
            putString("last_outcome", result.outcome.name)
            if (result.outcome == Outcome.SAVED || result.outcome == Outcome.EMPTY) putLong("last_success_ms", now)
        }
    }
}
