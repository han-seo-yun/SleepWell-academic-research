package app.sleepwell.sync

import android.content.Context
import android.content.pm.PackageManager
import android.health.connect.HealthConnectException
import android.health.connect.HealthConnectManager
import android.health.connect.HealthPermissions
import android.health.connect.ReadRecordsRequestUsingFilters
import android.health.connect.ReadRecordsResponse
import android.health.connect.TimeInstantRangeFilter
import android.health.connect.datatypes.DataOrigin
import android.health.connect.datatypes.ExerciseSessionRecord
import android.health.connect.datatypes.StepsRecord
import android.os.Build
import android.os.OutcomeReceiver
import androidx.annotation.RequiresApi
import kotlinx.coroutines.CancellationException
import java.time.Instant
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

data class SyncContextEvent(
    val id: String, val kind: String, val start: Instant, val end: Instant?,
    val value: Long?, val modifiedAt: Instant, val source: String = "samsung_health"
)

data class HealthContextReadResult(
    val events: List<SyncContextEvent>,
    val completeScopes: Set<String>,
    val failedScopes: Set<String> = emptySet()
)

class HealthConnectContextReader(private val context: Context) {
    fun isSupported() = Build.VERSION.SDK_INT >= 34
    fun hasStepsPermission() = has(HealthPermissions.READ_STEPS)
    fun hasExercisePermission() = has(HealthPermissions.READ_EXERCISE)

    private fun has(permission: String) = Build.VERSION.SDK_INT >= 34 &&
        context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    @RequiresApi(34)
    suspend fun readAvailableSamsungContext(from: Instant, to: Instant): HealthContextReadResult {
        val manager = context.getSystemService(HealthConnectManager::class.java)
            ?: error("이 기기에서 Health Connect를 사용할 수 없습니다.")
        val events = mutableListOf<SyncContextEvent>()
        val complete = mutableSetOf<String>()
        val failed = mutableSetOf<String>()
        if (hasStepsPermission()) {
            try {
                val rows = readAll(manager, StepsRecord::class.java, from, to)
                events += rows.asSequence().filter(::isSamsung).filter { it.startTime < it.endTime && !it.endTime.isAfter(to) }
                    .map { SyncContextEvent("steps:${it.metadata.id}", "steps", it.startTime, it.endTime,
                        it.count, it.metadata.lastModifiedTime) }
                complete += "steps"
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                failed += "steps"
            }
        }
        if (hasExercisePermission()) {
            try {
                val rows = readAll(manager, ExerciseSessionRecord::class.java, from, to)
                events += rows.asSequence().filter(::isSamsung).filter { it.startTime < it.endTime && !it.endTime.isAfter(to) }
                    .map { SyncContextEvent("exercise:${it.metadata.id}", "exercise", it.startTime, it.endTime,
                        null, it.metadata.lastModifiedTime) }
                complete += "exercise"
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                failed += "exercise"
            }
        }
        return HealthContextReadResult(events.sortedBy { it.start }, complete, failed)
    }

    @RequiresApi(34)
    private suspend fun <T : android.health.connect.datatypes.Record> readAll(
        manager: HealthConnectManager, type: Class<T>, from: Instant, to: Instant
    ): List<T> {
        val result = mutableListOf<T>()
        var token = -1L
        do {
            val builder = ReadRecordsRequestUsingFilters.Builder(type)
                .setTimeRangeFilter(TimeInstantRangeFilter.Builder().setStartTime(from).setEndTime(to).build())
                .setPageSize(5000)
                .addDataOrigins(DataOrigin.Builder().setPackageName(HealthConnectSleepReader.SAMSUNG_HEALTH_PACKAGE).build())
            if (token >= 0) builder.setPageToken(token) else builder.setAscending(true)
            val page = suspendCoroutine { continuation ->
                manager.readRecords(builder.build(), context.mainExecutor,
                    object : OutcomeReceiver<ReadRecordsResponse<T>, HealthConnectException> {
                        override fun onResult(value: ReadRecordsResponse<T>) = continuation.resume(value)
                        override fun onError(error: HealthConnectException) = continuation.resumeWithException(error)
                    })
            }
            result += page.records
            token = page.nextPageToken
        } while (token >= 0)
        return result
    }

    private fun isSamsung(record: android.health.connect.datatypes.Record) =
        record.metadata.id.isNotBlank() &&
            record.metadata.dataOrigin.packageName == HealthConnectSleepReader.SAMSUNG_HEALTH_PACKAGE
}
