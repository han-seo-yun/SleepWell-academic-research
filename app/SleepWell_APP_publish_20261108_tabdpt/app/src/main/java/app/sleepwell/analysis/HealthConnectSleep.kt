package app.sleepwell.analysis

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.time.Instant
import java.time.temporal.ChronoUnit

/*
 * 삼성 헬스 → 헬스 커넥트(Health Connect) → 앱 (소빈 · 2026-10-27, 시험 연결)
 *
 * 10/27 회의: "삼성 SDK 말고 안드로이드 자체 기능으로, CSV 없이 연동되는지 확인"
 *  - 헬스 커넥트 = 안드로이드의 건강 데이터 저장소. 안드로이드 14 이상은 기본 내장, 13 이하는 Play 스토어 앱.
 *  - 삼성 헬스가 갤럭시 워치 수면(단계 포함)을 헬스 커넥트에 써 주고, 우리 앱은 "수면 읽기" 권한으로 읽는다.
 *    → 삼성 헬스 데이터 SDK(파트너 승인 필요)를 거치지 않아도 됨.
 *  - 사용자 쪽 설정: 삼성 헬스 → 설정 → 헬스 커넥트 에서 삼성 헬스의 수면 쓰기(공유)가 켜져 있어야 함.
 *  - 기본으로는 **권한을 처음 허용한 날 기준 30일 전**까지만 읽을 수 있다 (헬스 커넥트 규칙). 분석은 7일이면 충분.
 *
 * 읽은 세션은 HealthConnectCsv 로 기존 수면 단계 CSV 형식으로 바꿔서, 지금 분석 길(파일 업로드와 같은 길)로 넘긴다.
 */
object HealthConnectSleep {

    /** 삼성 헬스 앱 패키지. 같은 수면이 다른 앱에서도 들어와 있으면 삼성 헬스 것만 쓴다 */
    const val SAMSUNG_HEALTH_PACKAGE = "com.sec.android.app.shealth"

    val PERMISSIONS: Set<String> = setOf(HealthPermission.getReadPermission(SleepSessionRecord::class))
    val BACKGROUND_PERMISSIONS: Set<String> = setOf(HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND)

    fun supportsBackgroundRead(context: Context): Boolean =
        HealthConnectClient.getOrCreate(context).features.getFeatureStatus(
            HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND
        ) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE

    enum class Availability { AVAILABLE, NEEDS_UPDATE, UNAVAILABLE }

    fun availability(context: Context): Availability = when (HealthConnectClient.getSdkStatus(context)) {
        HealthConnectClient.SDK_AVAILABLE -> Availability.AVAILABLE
        HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> Availability.NEEDS_UPDATE
        else -> Availability.UNAVAILABLE
    }

    suspend fun hasPermission(context: Context): Boolean =
        HealthConnectClient.getOrCreate(context).permissionController.getGrantedPermissions().containsAll(PERMISSIONS)

    data class ImportResult(
        val csv: String,
        val sessionCount: Int,
        /** 삼성 헬스가 쓴 기록인가 (false = 다른 앱 기록만 있음) */
        val fromSamsung: Boolean,
        val firstMs: Long?,
        val lastMs: Long?
    )

    /** 최근 [days]일 수면 세션을 읽어 CSV 로. 권한이 없으면 SecurityException 이 난다 (부르는 쪽에서 처리) */
    suspend fun readAsCsv(context: Context, days: Long = 30): ImportResult {
        val client = HealthConnectClient.getOrCreate(context)
        val end = Instant.now()
        val start = end.minus(days, ChronoUnit.DAYS)
        val all = mutableListOf<SleepSessionRecord>()
        var token: String? = null
        do {
            val response = client.readRecords(
                ReadRecordsRequest(
                    recordType = SleepSessionRecord::class,
                    timeRangeFilter = TimeRangeFilter.between(start, end),
                    pageToken = token
                )
            )
            all += response.records
            token = response.pageToken
        } while (!token.isNullOrEmpty())

        val samsung = all.filter { it.metadata.dataOrigin.packageName == SAMSUNG_HEALTH_PACKAGE }
        val use = if (samsung.isNotEmpty()) samsung else all
        val sessions = use.map { r ->
            HcSleepSession(
                id = "${r.metadata.dataOrigin.packageName}:${r.metadata.id}",
                startMs = r.startTime.toEpochMilli(),
                endMs = r.endTime.toEpochMilli(),
                offsetSeconds = r.startZoneOffset?.totalSeconds,
                stages = r.stages.map { s -> HcSleepStage(s.startTime.toEpochMilli(), s.endTime.toEpochMilli(), samsungCode(s.stage)) },
                source = if (r.metadata.dataOrigin.packageName == SAMSUNG_HEALTH_PACKAGE) "samsung_health" else "health_connect",
                observedAtMs = r.metadata.lastModifiedTime.toEpochMilli()
            )
        }
        return ImportResult(
            csv = HealthConnectCsv.toCsv(sessions, idPrefix = ""),
            sessionCount = sessions.size,
            fromSamsung = samsung.isNotEmpty(),
            firstMs = sessions.minOfOrNull { it.startMs },
            lastMs = sessions.maxOfOrNull { it.endMs }
        )
    }

    /** 헬스 커넥트 단계 → 삼성 코드. SLEEPING/UNKNOWN은 결측으로 보존한다. */
    private fun samsungCode(stage: Int): Int = when (stage) {
        SleepSessionRecord.STAGE_TYPE_AWAKE,
        SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED,
        SleepSessionRecord.STAGE_TYPE_OUT_OF_BED -> HealthConnectCsv.AWAKE
        SleepSessionRecord.STAGE_TYPE_DEEP -> HealthConnectCsv.DEEP
        SleepSessionRecord.STAGE_TYPE_REM -> HealthConnectCsv.REM
        SleepSessionRecord.STAGE_TYPE_LIGHT -> HealthConnectCsv.LIGHT
        else -> HealthConnectCsv.UNKNOWN
    }
}
