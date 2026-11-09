package app.sleepwell.analysis

import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** Runs in the app process even when no Activity is open. Android owns execution timing. */
class SleepSyncJobService : JobService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val runs = mutableMapOf<Int, Job>()

    override fun onStartJob(params: JobParameters): Boolean {
        runs[params.jobId]?.cancel()
        val work = scope.launch(start = CoroutineStart.LAZY) {
            val result = withTimeoutOrNull(60_000L) { BackgroundSleepSync.run(applicationContext) }
                ?: BackgroundSleepSync.SyncResult(BackgroundSleepSync.Outcome.RETRY)
            if (runs[params.jobId] !== currentCoroutineContext()[Job]) return@launch
            BackgroundSleepSync.recordResult(applicationContext, result)
            runs.remove(params.jobId)
            jobFinished(params, result.outcome == BackgroundSleepSync.Outcome.RETRY)
            if (result.outcome == BackgroundSleepSync.Outcome.BLOCKED) {
                getSystemService(JobScheduler::class.java).cancel(params.jobId)
            }
        }
        runs[params.jobId] = work
        work.start()
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean {
        runs.remove(params.jobId)?.cancel()
        return true
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }
}
