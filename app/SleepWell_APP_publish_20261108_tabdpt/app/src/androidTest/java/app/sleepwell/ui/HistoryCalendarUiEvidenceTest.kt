package app.sleepwell.ui

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.os.SystemClock
import android.test.InstrumentationTestCase
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.SavedResult
import app.sleepwell.analysis.SleepHistoryNight
import java.io.File
import java.time.Instant
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZoneId

/** In-memory UI evidence. It never opens MainActivity or touches the user's stores. */
@Suppress("DEPRECATION")
class HistoryCalendarUiEvidenceTest : InstrumentationTestCase() {
    fun testCalendarEmptySelectedAndDetailRenderInLightDarkLargeText() {
        val target = instrumentation.targetContext
        val output = File(target.cacheDir, "history-calendar-evidence").apply { mkdirs() }
        var activity: Activity? = null
        try {
            activity = instrumentation.startActivitySync(Intent().setClassName(
                "app.sleepwell", HealthPermissionsRationaleActivity::class.java.name
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            val host = activity as ComponentActivity
            listOf("empty-light", "selected-dark-large", "detail-dark-large").forEach { mode ->
                instrumentation.runOnMainSync {
                    host.setContent {
                        val dark = mode.contains("dark")
                        val density = LocalDensity.current
                        CompositionLocalProvider(LocalDensity provides Density(density.density, if (mode.contains("large")) 1.45f else 1f)) {
                            MaterialTheme(colorScheme = if (dark) darkColorScheme() else lightColorScheme()) {
                                CalendarFixture(mode)
                            }
                        }
                    }
                }
                instrumentation.waitForIdleSync(); SystemClock.sleep(200); instrumentation.waitForIdleSync()
                val bitmap = instrumentation.uiAutomation.takeScreenshot()
                assertNotNull(bitmap)
                File(output, "$mode.png").outputStream().use { stream ->
                    assertTrue(bitmap!!.compress(Bitmap.CompressFormat.PNG, 100, stream))
                }
                bitmap!!.recycle()
                assertTrue(File(output, "$mode.png").length() > 0)
            }
        } finally {
            activity?.let { opened -> instrumentation.runOnMainSync { opened.finish() } }
        }
    }
}

@Composable
private fun CalendarFixture(mode: String) {
    val zone = ZoneId.of("Asia/Seoul")
    val date = LocalDate.of(2026, 11, 5)
    val day = HistoryCalendarDay(date, listOf(fixtureNight()), listOf(fixtureResult()), emptyList())
    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).padding(16.dp)) {
        HistoryCalendar(
            month = YearMonth.of(2026, 11), today = date,
            selected = date.takeUnless { mode.startsWith("empty") },
            days = if (mode.startsWith("empty")) emptyMap() else mapOf(date to day),
            onPrevious = {}, onNext = {}, onToday = {}, onSelect = {}
        )
        if (mode.startsWith("selected")) SelectedDaySummary(day) {}
    }
    if (mode.startsWith("detail")) DayDetailSheet(day, zone, onDismiss = {}, onOpenSleep = {}, onOpenSaved = {})
}

private fun fixtureNight(): SleepHistoryNight {
    val bed = Instant.parse("2026-11-04T14:30:00Z").toEpochMilli()
    val wake = Instant.parse("2026-11-04T22:30:00Z").toEpochMilli()
    return SleepHistoryNight(
        day = "2026-11-05", bedtimeMs = bed, wakeMs = wake, intervalMinutes = 480,
        source = "test_fixture", sourceLabel = "검증 전용 기록", isManual = false,
        sleepLatencyMinutes = null, awakeMinutes = null, rested = null, fatigue = null,
        survey = null, observedAtMs = wake
    )
}

private fun fixtureResult(): SavedResult {
    val basis = Instant.parse("2026-11-04T22:30:00Z").toEpochMilli()
    return SavedResult(
        savedAtMs = basis + 60_000, source = "test_fixture", basisMs = basis, basisText = "11/5 07:30 기상",
        modelText = "23:30 취침 → 07:30 기상", recText = "07:30 기상 목표 → 23:15 전에 취침 · 수면 기회 8시간 15분",
        habitText = "23:40 취침 → 07:20 기상", statusText = "점진 적용", surveyText = "피로도 3/5",
        sValue = "0.52", cValue = "0.10", pValue = "0.62", sourceZoneId = "Asia/Seoul",
        dayDate = "2026-11-05", profileText = "성별·연령대·희망 수면시간 적용"
    )
}
