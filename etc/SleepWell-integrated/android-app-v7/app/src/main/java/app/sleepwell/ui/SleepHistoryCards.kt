package app.sleepwell.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.SleepHistoryNight
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

private val dateFormat = DateTimeFormatter.ofPattern("M월 d일 E요일")
private val timeFormat = DateTimeFormatter.ofPattern("HH:mm")

@Composable
fun RecentSleepOverview(nights: List<SleepHistoryNight>, zone: ZoneId) {
    val historyLine = MaterialTheme.colorScheme.outlineVariant
    val historyNote = MaterialTheme.colorScheme.onSurfaceVariant
    val latest = nights.firstOrNull()
    Card(
        modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
        border = BorderStroke(1.dp, historyLine)
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            Text("최근 주 수면", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            if (latest == null) {
                Text("표시할 주 수면 기록이 없어요. 불러온 기록 중 3시간 이상인 주 수면을 날짜별로 표시합니다.",
                    style = MaterialTheme.typography.bodyMedium)
            } else {
                Text(Instant.ofEpochMilli(latest.wakeMs).atZone(zone).format(dateFormat),
                    style = MaterialTheme.typography.labelLarge, color = historyNote)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("${shown(latest.bedtimeMs, zone)} 취침", style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.weight(1f))
                    Text("→", style = MaterialTheme.typography.titleMedium, color = historyNote,
                        modifier = Modifier.padding(horizontal = 4.dp))
                    Text("${shown(latest.wakeMs, zone)} 기상", style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.weight(1f), textAlign = TextAlign.End)
                }
                Text("침대에 있던 구간 ${duration(latest.intervalMinutes)} · ${latest.sourceLabel}",
                    style = MaterialTheme.typography.bodySmall, color = historyNote)
                val latestDay = java.time.LocalDate.now(zone)
                val recent = nights.filter {
                    Instant.ofEpochMilli(it.wakeMs).atZone(zone).toLocalDate().isAfter(latestDay.minusDays(7))
                }
                val previous = nights.filter {
                    val day = Instant.ofEpochMilli(it.wakeMs).atZone(zone).toLocalDate()
                    !day.isAfter(latestDay.minusDays(7)) && day.isAfter(latestDay.minusDays(14))
                }
                Text("최근 7일 ${recent.size}회 반영" + trend(recent, previous, zone)?.let { " · $it" }.orEmpty(),
                    style = MaterialTheme.typography.labelMedium)
                Text("이 구간은 수면 점수나 실제 수면시간을 뜻하지 않아요.",
                    style = MaterialTheme.typography.labelSmall, color = historyNote)
            }
        }
    }
}

@Composable
fun SleepHistoryNightCard(night: SleepHistoryNight, zone: ZoneId) {
    val historyLine = MaterialTheme.colorScheme.outlineVariant
    val historyNote = MaterialTheme.colorScheme.onSurfaceVariant
    Card(
        modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, historyLine)
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(Instant.ofEpochMilli(night.wakeMs).atZone(zone).format(dateFormat),
                    style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f))
                Text(night.sourceLabel, style = MaterialTheme.typography.labelMedium, color = historyNote,
                    textAlign = TextAlign.End)
            }
            Text("${shown(night.bedtimeMs, zone)} 취침 → ${shown(night.wakeMs, zone)} 기상",
                style = MaterialTheme.typography.titleMedium)
            Text("침대에 있던 구간 ${duration(night.intervalMinutes)}", style = MaterialTheme.typography.bodySmall)
            if (night.stageMinutes.isNotEmpty()) {
                val labels = listOf("awake" to "깨어 있음", "light" to "얕은 수면", "deep" to "깊은 수면", "rem" to "REM")
                    .mapNotNull { (key, label) -> night.stageMinutes[key]?.let { "$label ${it}분" } }
                val observed = night.stageObservedMinutes ?: 0
                Text(
                    "관측된 수면 단계 $observed/${night.intervalMinutes}분 · ${labels.joinToString(" · ")}" +
                        if (observed < night.intervalMinutes) " · 미관측 구간은 추정하지 않음" else "",
                    style = MaterialTheme.typography.bodySmall,
                    color = historyNote
                )
            }
            val measured = buildList {
                night.sleepLatencyMinutes?.let { add("입면까지 ${it}분") }
                night.awakeMinutes?.let { add("중간각성 ${it}분") }
            }
            Text(if (measured.isEmpty()) "세부 측정값 없음 · 수면 시각은 DATA33·개인화에 반영"
                else "측정·보완 · ${measured.joinToString(" · ")}",
                style = MaterialTheme.typography.bodySmall, color = historyNote)
            val answers = buildList {
                night.rested?.let { add("개운함 $it/5") }
                night.fatigue?.let { add("피로도 $it/${night.fatigueScaleMax ?: 7}") }
            }
            if (answers.isNotEmpty()) Text("설문 · ${answers.joinToString(" · ")}", style = MaterialTheme.typography.bodySmall)
            night.survey?.let { survey ->
                Text(
                    "보완 응답 · ${survey.summaryIn(zone, includeAlarm = false)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = historyNote
                )
            }
            if (night.isManual) Text("같은 기상일의 삼성 헬스 기록보다 직접 기록을 우선해 표시·반영합니다.",
                style = MaterialTheme.typography.labelSmall, color = historyNote)
        }
    }
}

private fun shown(ms: Long, zone: ZoneId) = Instant.ofEpochMilli(ms).atZone(zone).format(timeFormat)
private fun duration(minutes: Int) = "${minutes / 60}시간" + (minutes % 60).takeIf { it > 0 }?.let { " ${it}분" }.orEmpty()

private fun trend(recentNights: List<SleepHistoryNight>, previousNights: List<SleepHistoryNight>, zone: ZoneId): String? {
    if (recentNights.size < 2 || previousNights.size < 2) return null
    var delta = circularMean(recentNights.map { minute(it.bedtimeMs, zone) }) -
        circularMean(previousNights.map { minute(it.bedtimeMs, zone) })
    while (delta > 720) delta -= 1440
    while (delta < -720) delta += 1440
    val rounded = (delta / 5.0).roundToInt() * 5
    return when {
        rounded <= -10 -> "직전 7일보다 취침이 약 ${-rounded}분 빨라짐"
        rounded >= 10 -> "직전 7일보다 취침이 약 ${rounded}분 늦어짐"
        else -> "직전 7일과 취침 시각이 비슷함"
    }
}

private fun minute(ms: Long, zone: ZoneId): Int = Instant.ofEpochMilli(ms).atZone(zone).let { it.hour * 60 + it.minute }
private fun circularMean(values: List<Int>): Int {
    val angles = values.map { it * 2 * PI / 1440 }
    val angle = atan2(angles.sumOf { sin(it) }, angles.sumOf { cos(it) })
    return (((angle * 1440 / (2 * PI)) + 1440) % 1440).roundToInt()
}
