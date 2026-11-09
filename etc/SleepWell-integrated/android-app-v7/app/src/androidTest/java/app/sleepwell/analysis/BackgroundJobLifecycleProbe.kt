package app.sleepwell.analysis

import android.app.job.JobScheduler
import android.test.InstrumentationTestCase

/** Explicit manual probe, excluded from the *Test.kt suite. Shell runs/cancels job 53100 after this process exits. */
@Suppress("DEPRECATION")
class BackgroundJobLifecycleProbe : InstrumentationTestCase() {
    fun testRegisterProbeJob() {
        val context = instrumentation.targetContext
        val scheduler = context.getSystemService(JobScheduler::class.java)
        assertNull(scheduler.getPendingJob(PROBE_JOB_ID))
        assertEquals(JobScheduler.RESULT_SUCCESS, scheduler.schedule(BackgroundSleepSync.buildJob(context, PROBE_JOB_ID)))
    }

    companion object { const val PROBE_JOB_ID = 53100 }
}
