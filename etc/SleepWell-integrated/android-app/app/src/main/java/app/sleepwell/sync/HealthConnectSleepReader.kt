package app.sleepwell.sync

import android.content.Context
import android.health.connect.HealthConnectException
import android.health.connect.HealthConnectManager
import android.health.connect.ReadRecordsRequestUsingFilters
import android.health.connect.TimeInstantRangeFilter
import android.health.connect.datatypes.SleepSessionRecord
import android.health.connect.datatypes.DataOrigin
import android.os.Build
import android.os.OutcomeReceiver
import androidx.annotation.RequiresApi
import java.time.Instant
import java.time.Duration
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

data class SyncSleepStage(val start: Instant, val end: Instant, val stage: String)
data class SyncSleepSession(
    val id: String,
    val modifiedAt: Instant,
    val start: Instant,
    val end: Instant,
    val stages: List<SyncSleepStage>
)
data class HealthSleepReadResult(
    val sessions: List<SyncSleepSession>,
    val observedSourceIds: Set<String>,
    val skippedCount: Int
)

class HealthConnectSleepReader(private val context: Context) {
    fun isSupported(): Boolean = Build.VERSION.SDK_INT >= 34

    fun hasPermission(): Boolean = Build.VERSION.SDK_INT >= 34 &&
        context.checkSelfPermission(android.health.connect.HealthPermissions.READ_SLEEP) ==
        android.content.pm.PackageManager.PERMISSION_GRANTED

    @RequiresApi(34)
    suspend fun readCompleteSamsungSessions(from: Instant, to: Instant): HealthSleepReadResult {
        check(hasPermission()) { "Health Connect 수면 읽기 권한이 없습니다." }
        val manager = context.getSystemService(HealthConnectManager::class.java)
            ?: error("이 기기에서 Health Connect를 사용할 수 없습니다.")
        val result = mutableListOf<SleepSessionRecord>()
        var pageToken = -1L
        do {
            val builder = ReadRecordsRequestUsingFilters.Builder(SleepSessionRecord::class.java)
                .setTimeRangeFilter(TimeInstantRangeFilter.Builder().setStartTime(from).setEndTime(to).build())
                .setPageSize(5000)
                .addDataOrigins(DataOrigin.Builder().setPackageName(SAMSUNG_HEALTH_PACKAGE).build())
            if (pageToken >= 0) builder.setPageToken(pageToken) else builder.setAscending(true)
            val page = suspendCoroutine { continuation ->
                manager.readRecords(
                    builder.build(),
                    context.mainExecutor,
                    object : OutcomeReceiver<android.health.connect.ReadRecordsResponse<SleepSessionRecord>, HealthConnectException> {
                        override fun onResult(value: android.health.connect.ReadRecordsResponse<SleepSessionRecord>) =
                            continuation.resume(value)
                        override fun onError(error: HealthConnectException) =
                            continuation.resumeWithException(error)
                    }
                )
            }
            result += page.records
            pageToken = page.nextPageToken
        } while (pageToken >= 0)

        val now = Instant.now()
        val samsung = result.asSequence()
            .filter { it.metadata.dataOrigin.packageName == SAMSUNG_HEALTH_PACKAGE }
            .filter { !it.endTime.isAfter(now) && it.startTime.isBefore(it.endTime) }
            .filter { it.metadata.id.isNotBlank() }
            .toList()
        val observedIds = samsung.mapTo(hashSetOf()) { it.metadata.id }
        val eligible = samsung.asSequence()
            .filter { Duration.between(it.startTime, it.endTime) in MIN_DURATION..MAX_DURATION }
            .map { record ->
                var lastEnd = record.startTime
                val cleanStages = mutableListOf<SyncSleepStage>()
                var invalidStageLayout = false
                record.stages.asSequence()
                    .sortedBy { it.startTime }
                    .forEach { raw ->
                        if (raw.startTime.isBefore(record.startTime) || raw.endTime.isAfter(record.endTime) ||
                            !raw.startTime.isBefore(raw.endTime) || raw.startTime.isBefore(lastEnd)
                        ) {
                            invalidStageLayout = true
                            return@forEach
                        }
                        lastEnd = raw.endTime
                        val stage = SyncSleepStage(raw.startTime, raw.endTime, stageName(raw.type))
                        val previous = cleanStages.lastOrNull()
                        if (previous != null && previous.stage == stage.stage && previous.end == stage.start) {
                            cleanStages[cleanStages.lastIndex] = previous.copy(end = stage.end)
                        } else cleanStages += stage
                    }
                if (invalidStageLayout || cleanStages.size > 500) cleanStages.clear()
                SyncSleepSession(
                    id = record.metadata.id,
                    modifiedAt = record.metadata.lastModifiedTime,
                    start = record.startTime,
                    end = record.endTime,
                    stages = cleanStages
                )
            }
            .sortedBy { it.start }
            .toList()
        return HealthSleepReadResult(eligible, observedIds, samsung.size - eligible.size)
    }

    private fun stageName(type: Int): String = when (type) {
        SleepSessionRecord.StageType.STAGE_TYPE_AWAKE,
        SleepSessionRecord.StageType.STAGE_TYPE_AWAKE_IN_BED -> "awake"
        SleepSessionRecord.StageType.STAGE_TYPE_AWAKE_OUT_OF_BED -> "out_of_bed"
        SleepSessionRecord.StageType.STAGE_TYPE_SLEEPING -> "sleeping"
        SleepSessionRecord.StageType.STAGE_TYPE_SLEEPING_LIGHT -> "light"
        SleepSessionRecord.StageType.STAGE_TYPE_SLEEPING_DEEP -> "deep"
        SleepSessionRecord.StageType.STAGE_TYPE_SLEEPING_REM -> "rem"
        else -> "unknown"
    }

    companion object {
        const val SAMSUNG_HEALTH_PACKAGE = "com.sec.android.app.shealth"
        private val MIN_DURATION: Duration = Duration.ofMinutes(30)
        private val MAX_DURATION: Duration = Duration.ofHours(16)
    }
}
