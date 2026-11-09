package app.sleepwell.analysis

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.os.SystemClock
import android.test.InstrumentationTestCase
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.sleepwell.health.SleepStage
import app.sleepwell.health.SleepStageSegment
import app.sleepwell.ui.ConnectionStatusCard
import app.sleepwell.ui.LatestSleepCard
import app.sleepwell.ui.ModelRhythmCard
import app.sleepwell.ui.SleepTrendCard
import app.sleepwell.ui.SleepWellTheme
import app.sleepwell.ui.HealthPermissionsRationaleActivity
import app.sleepwell.ui.SleepHistoryTimetable
import app.sleepwell.ui.SleepHistoryDetailSheet
import java.io.File
import java.time.ZoneId
import java.time.Instant

/**
 * Screenshot fixture content, hosted only while this instrumentation test runs.
 *
 * The host is the target app's existing HealthPermissionsRationaleActivity because Android's
 * Instrumentation.startActivitySync intentionally refuses a test-APK activity in another process.
 * That host's onCreate is a pure static information view; this test replaces its composition in
 * memory only. It never opens MainActivity or reads/writes ProfileStore, Health Connect, or user data.
 */
@Composable
private fun V8UiEvidenceScreen(mode: String) {
    val zone = ZoneId.of("Asia/Seoul")
    val nowMs = FIXTURE_NOW
    var selectedNight by remember(mode) { mutableStateOf<SleepHistoryNight?>(null) }
    Column(
        Modifier.fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        Text("검증 전용 · 가상 예시", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(
            "화면 fixture는 테스트 중 메모리에만 존재합니다. 스크린샷은 격리된 캐시에만 저장하며 실제 수면 기록·프로필·동기화 저장소에는 접근하지 않습니다.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        HorizontalDivider()
        when (mode) {
            MODE_TIMETABLE -> EvidenceSection("주간 수면 시간표") {
                SleepHistoryTimetable(timetableNights(), zone, nowMs) { selectedNight = it }
            }
            MODE_TIMETABLE_EMPTY -> EvidenceSection("기록 없는 시간표") {
                SleepHistoryTimetable(emptyList(), zone, nowMs) { selectedNight = it }
            }
            MODE_EMPTY -> EvidenceSection("기록 없음") {
                LatestSleepCard(emptyList(), zone, onConnect = {}, nowMs = nowMs)
                ModelRhythmCard(null, nowMs)
            }
            MODE_TIMING -> EvidenceSection("취침·기상 시각만 있음") {
                LatestSleepCard(listOf(timingNight()), zone, onConnect = {}, nowMs = nowMs)
            }
            MODE_PARTIAL -> EvidenceSection("일부 단계만 관측됨") {
                LatestSleepCard(listOf(partialStageNight()), zone, onConnect = {}, nowMs = nowMs)
            }
            MODE_FULL -> EvidenceSection("수면 단계 전체 관측") {
                val nights = trendNights()
                LatestSleepCard(nights, zone, onConnect = {}, nowMs = nowMs)
                SleepTrendCard(nights, zone, nowMs)
            }
            MODE_MODEL -> EvidenceSection("모델 추정과 미래 예측") {
                ModelRhythmCard(modelDisplay(), nowMs)
            }
            MODE_CONNECTION -> EvidenceSection("연결 상태") {
                ConnectionStatusCard(null, false, false, 0, null, null, nowMs, zone, null, onOpen = {})
                ConnectionStatusCard(false, false, false, 0, null, null, nowMs, zone, null, onOpen = {})
                ConnectionStatusCard(true, true, false, 0, null, nowMs, nowMs, zone, null, onOpen = {})
                ConnectionStatusCard(true, false, true, 3, null, nowMs, nowMs, zone, null, onOpen = {})
                ConnectionStatusCard(true, false, false, 0, null, nowMs, nowMs, zone, null, onOpen = {})
                ConnectionStatusCard(true, false, false, 3, nowMs - 4 * DAY_MS, nowMs, nowMs, zone, null, onOpen = {})
                ConnectionStatusCard(true, false, false, 3, nowMs - DAY_MS, nowMs, nowMs, zone, null, onOpen = {})
            }
            else -> {
                EvidenceSection("기록 없음") { LatestSleepCard(emptyList(), zone, onConnect = {}, nowMs = nowMs) }
                EvidenceSection("취침·기상 시각만 있음") { LatestSleepCard(listOf(timingNight()), zone, onConnect = {}, nowMs = nowMs) }
                EvidenceSection("일부 단계만 관측됨") { LatestSleepCard(listOf(partialStageNight()), zone, onConnect = {}, nowMs = nowMs) }
                EvidenceSection("수면 단계 전체 관측과 추세") {
                    val nights = trendNights()
                    LatestSleepCard(nights, zone, onConnect = {}, nowMs = nowMs)
                    SleepTrendCard(nights, zone, nowMs)
                }
                EvidenceSection("모델 추정") { ModelRhythmCard(modelDisplay(), nowMs) }
                EvidenceSection("동기화 오류") {
                    ConnectionStatusCard(true, false, true, 3, null, nowMs, nowMs, zone, null, onOpen = {})
                }
            }
        }
    }
    selectedNight?.let { SleepHistoryDetailSheet(it, zone) { selectedNight = null } }
}

private fun timetableNights(): List<SleepHistoryNight> {
    val shift = DAY_MS
    return listOf(
        evidenceNight("2026-11-05", HOUR_1 + shift, HOUR_8 + shift, fullSegments(HOUR_1 + shift)),
        evidenceNight("2026-11-04", HOUR_1 + shift - DAY_MS, HOUR_8 + shift - DAY_MS, emptyList())
            .copy(isManual = true, source = "manual", sourceLabel = "검증용 직접 기록"),
        evidenceNight("2026-11-01", HOUR_1 + shift - 4 * DAY_MS, HOUR_8 + shift - 4 * DAY_MS, emptyList())
    )
}

@Composable
private fun EvidenceSection(title: String, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        content()
    }
}

private fun timingNight() = evidenceNight(
    day = "2026-11-04", start = HOUR_1, end = HOUR_8, segments = emptyList()
)

private fun partialStageNight() = evidenceNight(
    day = "2026-11-04", start = HOUR_1, end = HOUR_8,
    segments = listOf(
        stage(HOUR_1, 30, SleepStage.WAKE),
        stage(HOUR_1 + 30 * MINUTE_MS, 90, SleepStage.LIGHT),
        stage(HOUR_1 + 4 * HOUR_MS, 60, SleepStage.DEEP),
        stage(HOUR_1 + 6 * HOUR_MS, 30, SleepStage.REM)
    ),
    observedMinutes = 210
)

private fun trendNights(): List<SleepHistoryNight> = listOf(
    evidenceNight("2026-11-04", HOUR_1, HOUR_8, fullSegments(HOUR_1)),
    evidenceNight("2026-11-03", HOUR_1 - DAY_MS, HOUR_8 - DAY_MS, fullSegments(HOUR_1 - DAY_MS)),
    evidenceNight("2026-11-02", HOUR_1 - 2 * DAY_MS, HOUR_8 - 2 * DAY_MS, fullSegments(HOUR_1 - 2 * DAY_MS))
)

private fun evidenceNight(
    day: String,
    start: Long,
    end: Long,
    segments: List<SleepStageSegment>,
    observedMinutes: Int? = if (segments.isEmpty()) null else segments.sumOf { ((it.endMs - it.startMs) / MINUTE_MS).toInt() }
) = SleepHistoryNight(
    day = day,
    bedtimeMs = start,
    wakeMs = end,
    intervalMinutes = ((end - start) / MINUTE_MS).toInt(),
    source = "test_fixture",
    sourceLabel = "검증용 가상 예시",
    isManual = false,
    sleepLatencyMinutes = null,
    awakeMinutes = null,
    rested = null,
    fatigue = null,
    survey = null,
    observedAtMs = FIXTURE_NOW,
    stageMinutes = segments.groupBy { it.stage }.mapValues { (_, rows) ->
        rows.sumOf { ((it.endMs - it.startMs) / MINUTE_MS).toInt() }
    }.mapKeys { (stage, _) -> stage.name.lowercase() },
    stageObservedMinutes = observedMinutes,
    stageSegments = segments
)

private fun fullSegments(start: Long) = listOf(
    stage(start, 20, SleepStage.WAKE),
    stage(start + 20 * MINUTE_MS, 120, SleepStage.LIGHT),
    stage(start + 140 * MINUTE_MS, 100, SleepStage.DEEP),
    stage(start + 240 * MINUTE_MS, 80, SleepStage.REM),
    stage(start + 320 * MINUTE_MS, 80, SleepStage.LIGHT),
    stage(start + 400 * MINUTE_MS, 20, SleepStage.WAKE)
)

private fun stage(start: Long, minutes: Int, type: SleepStage) = SleepStageSegment(
    startMs = start, endMs = start + minutes * MINUTE_MS, stage = type
)

private fun modelDisplay(): AnalysisDisplay {
    val history = (0..4).map { index ->
        ChartPoint(HOUR_1 + index * HOUR_MS, 0.20 + index * 0.08, 0.50 - index * 0.03, 0.42 + index * 0.03)
    }
    val future = (1..4).map { index ->
        ChartPoint(HOUR_8 + index * HOUR_MS, 0.55 + index * 0.06, 0.35 - index * 0.02, 0.58 + index * 0.04)
    }
    return AnalysisDisplay(
        modelLabel = "검증용 모델 예시",
        isTemporaryModel = true,
        referenceMs = HOUR_8,
        readings = emptyList(),
        history = history,
        future = future,
        futureIsTemporary = true,
        sleepBands = listOf(SleepBand(HOUR_1, HOUR_8)),
        easiestSleepMs = null,
        recentSleeps = emptyList(),
        recommendationText = "검증 전용",
        graphStartMs = HOUR_1,
        graphEndMs = HOUR_8 + 4 * HOUR_MS
    )
}

/**
 * The target host is only a transient window. Screenshots are written only to its isolated cache
 * directory: app.sleepwell/cache/v8-ui-evidence; fixture records never leave memory.
 */
@Suppress("DEPRECATION")
class V8UiEvidenceTest : InstrumentationTestCase() {
    fun testEvidenceStatesRenderAndWriteOnlyToIsolatedCache() {
        val targetContext = instrumentation.targetContext
        assertEquals("Evidence host must be the target package", "app.sleepwell", targetContext.packageName)
        val cacheDir = targetContext.cacheDir.canonicalFile
        val outputDir = File(cacheDir, "v8-ui-evidence").apply { mkdirs() }.canonicalFile
        assertTrue("Evidence cache directory must be available", outputDir.isDirectory)
        assertEquals("Evidence output must remain inside the isolated cache", cacheDir, outputDir.parentFile)
        var activity: Activity? = null
        try {
            activity = instrumentation.startActivitySync(
                Intent().setClassName("app.sleepwell", HealthPermissionsRationaleActivity::class.java.name)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            assertTrue("The static, storage-free host must launch", activity is HealthPermissionsRationaleActivity)
            val composeHost = activity as ComponentActivity
            EVIDENCE_MODES.forEach { mode ->
                instrumentation.runOnMainSync {
                    composeHost.setContent { SleepWellTheme { V8UiEvidenceScreen(mode) } }
                }
                instrumentation.waitForIdleSync()
                // Give Compose one bounded display interval after setContent before capturing.
                SystemClock.sleep(250L)
                instrumentation.waitForIdleSync()
                assertNotNull("$mode evidence activity must launch", activity)
                assertFalse("$mode evidence activity must be alive", activity!!.isFinishing)
                instrumentation.runOnMainSync { assertTrue(activity!!.window.decorView.isShown) }
                captureEvidence(outputDir, mode)
            }
        } finally {
            activity?.let { opened -> instrumentation.runOnMainSync { opened.finish() } }
        }
    }
}

private fun V8UiEvidenceTest.captureEvidence(outputDir: File, mode: String) {
    val output = File(outputDir, "v8-$mode.png")
    val bitmap = instrumentation.uiAutomation.takeScreenshot()
    junit.framework.Assert.assertNotNull("$mode screenshot must be available", bitmap)
    val screenshot = bitmap ?: return
    try {
        output.outputStream().use { stream ->
            junit.framework.Assert.assertTrue(
                "$mode screenshot must encode",
                screenshot.compress(Bitmap.CompressFormat.PNG, 100, stream)
            )
        }
    } finally {
        screenshot.recycle()
    }
    junit.framework.Assert.assertTrue("$mode screenshot must be non-empty", output.length() > 0L)
}

private const val MINUTE_MS = 60_000L
private const val HOUR_MS = 60 * MINUTE_MS
private const val DAY_MS = 24 * HOUR_MS
private val FIXTURE_NOW = Instant.parse("2026-11-05T00:00:00Z").toEpochMilli()
private val HOUR_1 = Instant.parse("2026-11-03T14:30:00Z").toEpochMilli() // 23:30 KST
private val HOUR_8 = HOUR_1 + 7 * HOUR_MS
private const val MODE_EMPTY = "empty"
private const val MODE_TIMING = "timing"
private const val MODE_PARTIAL = "partial"
private const val MODE_FULL = "full"
private const val MODE_MODEL = "model"
private const val MODE_CONNECTION = "connection"
private const val MODE_TIMETABLE = "timetable"
private const val MODE_TIMETABLE_EMPTY = "timetable-empty"
private val EVIDENCE_MODES = listOf(
    MODE_EMPTY,
    MODE_TIMING,
    MODE_PARTIAL,
    MODE_FULL,
    MODE_MODEL,
    MODE_CONNECTION,
    MODE_TIMETABLE,
    MODE_TIMETABLE_EMPTY
)
