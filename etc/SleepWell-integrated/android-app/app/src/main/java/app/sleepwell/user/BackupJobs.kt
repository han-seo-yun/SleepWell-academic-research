package app.sleepwell.user

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import java.util.concurrent.atomic.AtomicBoolean
import java.time.Duration
import java.time.ZonedDateTime

object BackupScheduler {
    private const val DAILY_JOB = 3701
    private const val RETRY_JOB = 3702

    fun schedule(context: Context, autoBackup: Boolean) {
        val scheduler = context.getSystemService(JobScheduler::class.java)
        if (!autoBackup) {
            scheduler.cancel(DAILY_JOB); scheduler.cancel(RETRY_JOB); return
        }
        val now = ZonedDateTime.now(UserRepository.ZONE)
        var next = now.toLocalDate().atTime(20, 0).atZone(UserRepository.ZONE)
        if (!next.isAfter(now)) next = next.plusDays(1)
        scheduler.schedule(JobInfo.Builder(DAILY_JOB, ComponentName(context, DailyBackupJobService::class.java))
            .setPersisted(true).setMinimumLatency(Duration.between(now, next).toMillis().coerceAtLeast(1)).build())
        scheduleRetry(context)
    }

    fun scheduleRetry(context: Context) {
        context.getSystemService(JobScheduler::class.java).schedule(
            JobInfo.Builder(RETRY_JOB, ComponentName(context, BackupRetryJobService::class.java))
                .setPersisted(true).setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
                .setMinimumLatency(15L * 60 * 1000).setBackoffCriteria(30L * 60 * 1000, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .build()
        )
    }
}

abstract class CancellableBackupJob : JobService() {
    @Volatile private var worker: Thread? = null
    @Volatile private var cancelled = AtomicBoolean(false)
    protected abstract fun runJob(manager: BackupManager, cancelled: AtomicBoolean)
    protected open fun afterFinished() = Unit

    override fun onStartJob(params: JobParameters): Boolean {
        val runCancelled = AtomicBoolean(false)
        cancelled = runCancelled
        worker = Thread {
            val reschedule = runCatching { runJob(BackupManager(applicationContext), runCancelled) }.isFailure
            if (!runCancelled.get()) {
                jobFinished(params, reschedule)
                if (!reschedule) afterFinished()
            }
        }.also { it.start() }
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean {
        cancelled.set(true)
        worker?.interrupt()
        worker = null
        return true // 큐는 성공 ACK 전까지 유지되므로 다음 기회에 안전하게 재시도한다.
    }
}

class DailyBackupJobService : CancellableBackupJob() {
    override fun runJob(manager: BackupManager, cancelled: AtomicBoolean) {
        if (!cancelled.get()) manager.createTodaySnapshot()
        if (!cancelled.get()) BackupScheduler.scheduleRetry(applicationContext)
    }

    override fun afterFinished() = BackupScheduler.schedule(
        applicationContext,
        UserSecureStore(applicationContext).load().autoBackup
    )
}

class BackupRetryJobService : CancellableBackupJob() {
    override fun runJob(manager: BackupManager, cancelled: AtomicBoolean) {
        if (!cancelled.get() && manager.pendingCount() > 0) manager.sendPending()
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            BackupScheduler.schedule(context, UserSecureStore(context).load().autoBackup)
        }
    }
}
