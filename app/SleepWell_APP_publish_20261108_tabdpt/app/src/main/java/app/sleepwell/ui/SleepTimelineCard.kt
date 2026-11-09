package app.sleepwell.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.SleepHistoryNight
import app.sleepwell.health.SleepStage
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

private val timelineDate = DateTimeFormatter.ofPattern("M월 d일 E요일", Locale.KOREAN)
private val timelineTime = DateTimeFormatter.ofPattern("HH:mm", Locale.KOREAN)
private val timelineDateTime = DateTimeFormatter.ofPattern("M/d HH:mm", Locale.KOREAN)

@Composable
fun LatestSleepCard(
    nights: List<SleepHistoryNight>,
    zone: ZoneId,
    onConnect: () -> Unit,
    nowMs: Long = System.currentTimeMillis(),
    title: String = "최근 수면"
) {
    var selectedDay by rememberSaveable { mutableStateOf<String?>(null) }
    val night = nights.firstOrNull { it.day == selectedDay } ?: nights.firstOrNull()
    val index = night?.let(nights::indexOf) ?: -1
    val border = MaterialTheme.colorScheme.outlineVariant
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(24.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
        border = BorderStroke(1.dp, border)
    ) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Column(Modifier.weight(1f)) {
                    Text(title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    Text(
                        night?.let { Instant.ofEpochMilli(it.wakeMs).atZone(zone).format(timelineDate) }
                            ?: Instant.ofEpochMilli(nowMs).atZone(zone).format(timelineDate),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                if (nights.size > 1) {
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        OutlinedButton(
                            onClick = { nights.getOrNull(index + 1)?.let { selectedDay = it.day } },
                            enabled = index in 0 until nights.lastIndex,
                            modifier = Modifier.size(48.dp).semantics {
                                contentDescription = nights.getOrNull(index + 1)?.let {
                                    "이전 수면 기록, ${Instant.ofEpochMilli(it.wakeMs).atZone(zone).format(timelineDate)}"
                                } ?: "이전 수면 기록 없음"
                            },
                            contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp)
                        ) { Text("‹", style = MaterialTheme.typography.titleLarge) }
                        OutlinedButton(
                            onClick = { nights.getOrNull(index - 1)?.let { selectedDay = it.day } },
                            enabled = index > 0,
                            modifier = Modifier.size(48.dp).semantics {
                                contentDescription = nights.getOrNull(index - 1)?.let {
                                    "다음 수면 기록, ${Instant.ofEpochMilli(it.wakeMs).atZone(zone).format(timelineDate)}"
                                } ?: "다음 수면 기록 없음"
                            },
                            contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp)
                        ) { Text("›", style = MaterialTheme.typography.titleLarge) }
                    }
                }
            }

            if (night == null) {
                EmptyTimeline()
                Text("표시할 주 수면 기록이 아직 없어요", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(
                    "3시간 이상인 주 수면이 들어오면 취침·기상 구간이 여기에 표시돼요. 짧은 수면이나 낮잠을 기록 없음으로 바꾸지는 않습니다.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Button(onClick = onConnect, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Text("연결·데이터 상태 확인")
                }
            } else {
                val isPastSelection = index > 0
                val isStale = nowMs - night.wakeMs > 3L * 24 * 60 * 60 * 1_000
                if (isPastSelection || isStale) {
                    Text(
                        listOfNotNull(if (isPastSelection) "과거 기록" else null, if (isStale) "3일 이상 지난 기록" else null)
                            .joinToString(" · "),
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.tertiary
                    )
                }
                if (night.stageSegments.isEmpty()) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Metric("침대에 있던 구간", durationText(night.intervalMinutes), Modifier.weight(1f))
                        Metric(
                            "취침 → 기상",
                            "${formatEndpoint(night, night.bedtimeMs, zone)} → ${formatEndpoint(night, night.wakeMs, zone)}",
                            Modifier.weight(1f)
                        )
                    }
                } else {
                    val observedSleep = night.stageSegments
                        .filter { it.stage.isSleep }
                        .sumOf { it.endMs - it.startMs }
                        .div(60_000L).toInt()
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Metric("관측된 수면", durationText(observedSleep), Modifier.weight(1f))
                        Metric("침대에 있던 구간", durationText(night.intervalMinutes), Modifier.weight(1f))
                    }
                    Text(
                        "${formatEndpoint(night, night.bedtimeMs, zone)} 취침 → ${formatEndpoint(night, night.wakeMs, zone)} 기상" +
                            if (isPartialStageObservation(night)) " · 일부 단계만 관측" else "",
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
                if (night.stageSegments.isEmpty()) {
                    IntervalTimeline(night, zone)
                    Text(
                        "수면 단계가 없는 기록이에요. 표시된 막대는 측정된 단계가 아니라 취침·기상 시각 사이의 구간입니다.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                } else {
                    StageTimeline(night, zone)
                    StageLegend()
                    val observed = night.stageObservedMinutes ?: 0
                    Text(
                        "관측된 단계 ${durationText(observed)} / 전체 구간 ${durationText(night.intervalMinutes)}" +
                            if (isPartialStageObservation(night)) " · 빈 구간은 추정하지 않음" else "",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Text("출처 ${night.sourceLabel}", style = MaterialTheme.typography.labelMedium)
            }
        }
    }
}

@Composable
private fun Metric(label: String, value: String, modifier: Modifier = Modifier) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun EmptyTimeline() {
    val line = MaterialTheme.colorScheme.outlineVariant
    Canvas(
        Modifier.fillMaxWidth().height(96.dp).semantics {
            contentDescription = "수면 기록이 들어오면 취침과 기상 구간을 표시할 빈 그래프"
        }
    ) {
        val y = size.height / 2
        drawLine(line, Offset(0f, y), Offset(size.width, y), 4.dp.toPx())
        repeat(5) { i ->
            val x = size.width * i / 4f
            drawLine(line, Offset(x, y - 8.dp.toPx()), Offset(x, y + 8.dp.toPx()), 1.dp.toPx())
        }
    }
}

@Composable
private fun IntervalTimeline(night: SleepHistoryNight, zone: ZoneId) {
    var selectedMs by remember(night.day) { mutableStateOf<Long?>(null) }
    var widthPx by remember { mutableIntStateOf(1) }
    fun choose(x: Float) {
        val ratio = (x / widthPx.coerceAtLeast(1)).coerceIn(0f, 1f)
        selectedMs = night.bedtimeMs + ((night.wakeMs - night.bedtimeMs) * ratio).toLong()
    }
    val fill = MaterialTheme.colorScheme.primary.copy(alpha = 0.72f)
    val track = MaterialTheme.colorScheme.surfaceContainerHighest
    Canvas(
        Modifier.fillMaxWidth().height(76.dp).onSizeChanged { widthPx = it.width }.pointerInput(night.day) {
            awaitEachGesture {
                var change = awaitFirstDown(requireUnconsumed = false)
                choose(change.position.x)
                while (change.pressed) {
                    change = awaitPointerEvent().changes.firstOrNull() ?: break
                    choose(change.position.x)
                }
            }
        }.semantics {
            contentDescription = "${formatEndpoint(night, night.bedtimeMs, zone)}부터 ${formatEndpoint(night, night.wakeMs, zone)}까지, " +
                "침대에 있던 구간 ${durationText(night.intervalMinutes)}, 수면 단계 없음"
        }
    ) {
        val top = 22.dp.toPx()
        val h = 22.dp.toPx()
        drawRoundRect(track, Offset(0f, top), Size(size.width, h), CornerRadius(11.dp.toPx()))
        drawRoundRect(fill, Offset(0f, top), Size(size.width, h), CornerRadius(11.dp.toPx()))
        drawLine(fill, Offset(0f, top - 8.dp.toPx()), Offset(0f, top + h + 8.dp.toPx()), 2.dp.toPx())
        drawLine(fill, Offset(size.width, top - 8.dp.toPx()), Offset(size.width, top + h + 8.dp.toPx()), 2.dp.toPx())
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(formatEndpoint(night, night.bedtimeMs, zone), style = MaterialTheme.typography.labelMedium)
        Text(formatEndpoint(night, night.wakeMs, zone), style = MaterialTheme.typography.labelMedium)
    }
    selectedMs?.let {
        Text("선택한 시각 ${formatTime(it, zone)} · 수면 단계는 기록되지 않음", style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
    }
}

@Composable
private fun StageTimeline(night: SleepHistoryNight, zone: ZoneId) {
    var selected by remember(night.day) { mutableIntStateOf(-1) }
    var widthPx by remember { mutableIntStateOf(1) }
    val segments = night.stageSegments
    val start = night.bedtimeMs
    val end = night.wakeMs.coerceAtLeast(start + 1)
    fun choose(x: Float) {
        val ms = start + ((x / widthPx.coerceAtLeast(1)) * (end - start)).toLong()
        selected = segments.indexOfFirst { ms in it.startMs until it.endMs }
    }
    val track = MaterialTheme.colorScheme.surfaceContainerHighest
    val colors = stageColors()
    val selectionOutline = MaterialTheme.colorScheme.onSurface
    Canvas(
        Modifier.fillMaxWidth().height(104.dp).onSizeChanged { widthPx = it.width }.pointerInput(night.day) {
            awaitEachGesture {
                var change = awaitFirstDown(requireUnconsumed = false)
                choose(change.position.x)
                while (change.pressed) {
                    change = awaitPointerEvent().changes.firstOrNull() ?: break
                    choose(change.position.x)
                }
            }
        }.semantics {
            contentDescription = stageDescription(night, zone)
        }
    ) {
        val top = 28.dp.toPx()
        val h = 32.dp.toPx()
        drawRoundRect(track, Offset(0f, top), Size(size.width, h), CornerRadius(10.dp.toPx()))
        segments.forEachIndexed { i, segment ->
            val x0 = size.width * (segment.startMs.coerceAtLeast(start) - start) / (end - start).toFloat()
            val x1 = size.width * (segment.endMs.coerceAtMost(end) - start) / (end - start).toFloat()
            if (x1 > x0) {
                drawRect(colors.getValue(segment.stage), Offset(x0, top), Size((x1 - x0).coerceAtLeast(1f), h))
                if (selected == i) drawRect(
                    selectionOutline, Offset(x0, top - 3.dp.toPx()),
                    Size((x1 - x0).coerceAtLeast(2f), h + 6.dp.toPx()),
                    style = androidx.compose.ui.graphics.drawscope.Stroke(2.dp.toPx())
                )
            }
        }
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(formatEndpoint(night, start, zone), style = MaterialTheme.typography.labelMedium)
        Text(formatEndpoint(night, end, zone), style = MaterialTheme.typography.labelMedium)
    }
    segments.getOrNull(selected)?.let { segment ->
        Text(
            "${stageLabel(segment.stage)} · ${formatTime(segment.startMs, zone)}–${formatTime(segment.endMs, zone)}",
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun StageLegend() {
    val colors = stageColors()
    FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        SleepStage.DISPLAY_ORDER.forEach { stage ->
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                Canvas(Modifier.size(10.dp)) { drawCircle(colors.getValue(stage)) }
                Text(stageLabel(stage), style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}

@Composable
private fun stageColors() = mapOf(
    SleepStage.WAKE to MaterialTheme.colorScheme.tertiary,
    SleepStage.REM to Color(0xFF8C6CCB),
    SleepStage.LIGHT to Color(0xFF5B8FCF),
    SleepStage.DEEP to MaterialTheme.colorScheme.primary
)

private fun stageLabel(stage: SleepStage) = when (stage) {
    SleepStage.WAKE -> "깨어 있음"
    SleepStage.REM -> "REM"
    SleepStage.LIGHT -> "얕은 수면"
    SleepStage.DEEP -> "깊은 수면"
    SleepStage.UNKNOWN -> "미관측"
}

private fun stageDescription(night: SleepHistoryNight, zone: ZoneId): String {
    val summary = SleepStage.DISPLAY_ORDER.mapNotNull { stage ->
        night.stageMinutes[stage.name.lowercase()]?.let { "${stageLabel(stage)} ${durationText(it)}" }
    }
    return "${formatEndpoint(night, night.bedtimeMs, zone)}부터 ${formatEndpoint(night, night.wakeMs, zone)}까지 수면 단계 그래프. " +
        summary.joinToString(", ")
}

internal fun formatTime(ms: Long, zone: ZoneId): String = Instant.ofEpochMilli(ms).atZone(zone).format(timelineTime)
private fun formatEndpoint(night: SleepHistoryNight, ms: Long, zone: ZoneId): String {
    val bedDay = Instant.ofEpochMilli(night.bedtimeMs).atZone(zone).toLocalDate()
    val wakeDay = Instant.ofEpochMilli(night.wakeMs).atZone(zone).toLocalDate()
    val formatter = if (bedDay == wakeDay) timelineTime else timelineDateTime
    return Instant.ofEpochMilli(ms).atZone(zone).format(formatter)
}
internal fun durationText(minutes: Int): String = "${minutes / 60}시간" +
    (minutes % 60).takeIf { it > 0 }?.let { " ${it}분" }.orEmpty()

private fun isPartialStageObservation(night: SleepHistoryNight): Boolean {
    val intervalMs = (night.wakeMs - night.bedtimeMs).coerceAtLeast(0L)
    return night.stageObservedCoverageMs?.let { it < intervalMs }
        ?: ((night.stageObservedMinutes ?: 0) * 60_000L < intervalMs)
}
