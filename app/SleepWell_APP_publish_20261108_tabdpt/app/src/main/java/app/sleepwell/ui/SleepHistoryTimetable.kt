package app.sleepwell.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.FatigueScale
import app.sleepwell.analysis.SleepHistoryNight
import app.sleepwell.analysis.SleepTimetableData
import app.sleepwell.analysis.SleepTimetablePeriod
import app.sleepwell.analysis.SleepTimetableRow
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.ceil

private val timetableRangeDate = DateTimeFormatter.ofPattern("yyyy년 M월 d일", Locale.KOREAN)
private val timetableRowDate = DateTimeFormatter.ofPattern("M/d\nE", Locale.KOREAN)
private val timetableClock = DateTimeFormatter.ofPattern("HH:mm", Locale.KOREAN)

/**
 * Read-only history view. It projects the already selected main-sleep records without writing,
 * reclassifying sessions, or changing the DATA33/personalisation inputs.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SleepHistoryTimetable(
    nights: List<SleepHistoryNight>,
    zone: ZoneId,
    nowMs: Long = System.currentTimeMillis(),
    onSelect: (SleepHistoryNight) -> Unit
) {
    val today = remember(zone, nowMs) { Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate() }
    val latestDate = remember(nights, zone) {
        nights.maxOfOrNull { Instant.ofEpochMilli(it.wakeMs).atZone(zone).toLocalDate() }
    }
    var days by rememberSaveable { mutableIntStateOf(7) }
    var endDateText by rememberSaveable(zone.id, today) { mutableStateOf(today.toString()) }
    val endDate = runCatching { LocalDate.parse(endDateText) }.getOrDefault(today).coerceAtMost(today)
    val period = remember(nights, zone, endDate, days) {
        SleepTimetableData.period(nights, zone, endDate, days)
    }
    val pageStep = days.toLong()
    val rangeText = "${period.startDate.year}.${period.startDate.monthValue}.${period.startDate.dayOfMonth} – " +
        (if (period.startDate.year != period.endDate.year) "${period.endDate.year}." else "") +
        "${period.endDate.monthValue}.${period.endDate.dayOfMonth}"

    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("수면 시간표", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Text(rangeText, style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                OutlinedButton(
                    onClick = { endDateText = endDate.minusDays(pageStep).toString() },
                    modifier = Modifier.size(48.dp).semantics { contentDescription = "이전 기간" },
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp)
                ) { Text("‹", style = MaterialTheme.typography.titleLarge) }
                OutlinedButton(
                    onClick = { endDateText = endDate.plusDays(pageStep).coerceAtMost(today).toString() },
                    enabled = endDate < today,
                    modifier = Modifier.size(48.dp).semantics { contentDescription = "다음 기간" },
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp)
                ) { Text("›", style = MaterialTheme.typography.titleLarge) }
            }
        }

        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf(7, 30).forEach { option ->
                FilterChip(
                    selected = days == option,
                    onClick = { days = option },
                    label = { Text("${option}일") },
                    modifier = Modifier.heightIn(min = 48.dp)
                )
            }
            FilterChip(
                selected = endDate == today,
                onClick = { endDateText = today.toString() },
                label = { Text("오늘까지") },
                modifier = Modifier.heightIn(min = 48.dp)
            )
            latestDate?.takeIf { it != today && it != endDate }?.let { latest ->
                FilterChip(
                    selected = false,
                    onClick = { endDateText = latest.coerceAtMost(today).toString() },
                    label = { Text("최근 기록") },
                    modifier = Modifier.heightIn(min = 48.dp)
                )
            }
        }

        PeriodSummary(period)
        Text(
            "취침 → 기상 · 기록 구간",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        TimetableAxis(period.axisStartMinute, period.axisEndMinute)
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            period.rows.forEach { row ->
                TimetableRow(row, period, zone, onSelect)
            }
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            TimetableLegend("가져온 기록", outlined = false)
            TimetableLegend("직접 기록", outlined = true)
        }
        Text(
            "막대는 취침부터 기상까지의 구간이며 실제 수면시간과 다를 수 있어요. 날짜를 누르면 관측된 단계·설문·출처를 볼 수 있어요. 기록 없는 날은 평균에서 제외합니다.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PeriodSummary(period: SleepTimetablePeriod) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        shape = RoundedCornerShape(18.dp)
    ) {
        FlowRow(
            Modifier.fillMaxWidth().padding(14.dp),
            horizontalArrangement = Arrangement.spacedBy(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            SummaryValue("기록된 날짜", "${period.recordedDays}일")
            SummaryValue("평균 기록 구간", period.averageIntervalMinutes?.let(::timetableDurationText) ?: "기록 없음")
        }
    }
}

@Composable
private fun SummaryValue(label: String, value: String) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun TimetableAxis(startMinute: Int, endMinute: Int) {
    val ticks = remember(startMinute, endMinute) { axisTicks(startMinute, endMinute) }
    val labelColor = MaterialTheme.colorScheme.onSurfaceVariant
    val density = LocalDensity.current
    val labelStyle = MaterialTheme.typography.labelSmall
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
        Spacer(Modifier.width(58.dp))
        Canvas(
            Modifier.weight(1f).height(52.dp).semantics {
                contentDescription = "시간축 " + ticks.joinToString { axisLabel(it).replace('\n', ' ') }
            }
        ) {
            val paint = android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
                color = labelColor.toArgb()
                textSize = with(density) { labelStyle.fontSize.toPx() }
                textAlign = android.graphics.Paint.Align.CENTER
            }
            val span = (endMinute - startMinute).coerceAtLeast(1).toFloat()
            val lineHeight = paint.fontMetrics.run { bottom - top }
            ticks.forEach { minute ->
                val lines = axisLabel(minute).split('\n')
                val fraction = ((minute - startMinute) / span).coerceIn(0f, 1f)
                val maxLineWidth = lines.maxOf(paint::measureText)
                val x = (size.width * fraction).coerceIn(maxLineWidth / 2f, size.width - maxLineWidth / 2f)
                val firstBaseline = size.height - paint.fontMetrics.bottom - (lines.size - 1) * lineHeight
                lines.forEachIndexed { index, line ->
                    drawContext.canvas.nativeCanvas.drawText(line, x, firstBaseline + index * lineHeight, paint)
                }
            }
        }
    }
}

@Composable
private fun TimetableRow(
    row: SleepTimetableRow,
    period: SleepTimetablePeriod,
    zone: ZoneId,
    onSelect: (SleepHistoryNight) -> Unit
) {
    if (row.nights.isEmpty()) {
        Row(
            Modifier.fillMaxWidth().heightIn(min = 56.dp).clearAndSetSemantics {
                contentDescription = "${row.date.format(timetableRangeDate)}, 기록 없음"
            }.padding(vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.width(58.dp)) {
                Text(row.date.format(timetableRowDate), style = MaterialTheme.typography.labelMedium)
            }
            Box(Modifier.weight(1f).height(48.dp), contentAlignment = Alignment.CenterStart) {
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                Text("기록 없음", style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 8.dp))
            }
        }
    } else {
        Column {
            row.nights.sortedBy { it.bedtimeMs }.forEachIndexed { index, night ->
                val description = "${row.date.format(timetableRangeDate)}, " +
                    "${timetableTime(night.bedtimeMs, zone)}부터 ${timetableTime(night.wakeMs, zone)}까지 " +
                    "기록 구간 ${timetableDurationText(night.intervalMinutes)}, ${night.sourceLabel}"
                Row(
                    Modifier.fillMaxWidth().heightIn(min = 56.dp)
                        .clickable(role = Role.Button) { onSelect(night) }
                        .clearAndSetSemantics {
                            contentDescription = description
                            role = Role.Button
                            onClick(label = "수면 상세 보기") { onSelect(night); true }
                        }
                        .padding(vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(Modifier.width(58.dp)) {
                        Text(if (index == 0) row.date.format(timetableRowDate) else "↳ ${index + 1}번째",
                            style = MaterialTheme.typography.labelMedium)
                        if (night.isManual) Text("직접", style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.tertiary)
                    }
                    TimetableBars(
                        SleepTimetableRow(row.date, listOf(night)), period, zone,
                        Modifier.weight(1f).height(48.dp)
                    )
                }
            }
        }
    }
}

@Composable
private fun TimetableBars(
    row: SleepTimetableRow,
    period: SleepTimetablePeriod,
    zone: ZoneId,
    modifier: Modifier
) {
    val autoColor = MaterialTheme.colorScheme.secondary
    val manualColor = MaterialTheme.colorScheme.tertiary
    val grid = MaterialTheme.colorScheme.outlineVariant
    val background = MaterialTheme.colorScheme.surfaceContainerHighest
    val axisSpan = (period.axisEndMinute - period.axisStartMinute).coerceAtLeast(1).toFloat()
    Canvas(modifier) {
        drawRoundRect(background, Offset(0f, size.height / 2f - 1.dp.toPx()),
            Size(size.width, 2.dp.toPx()), CornerRadius(1.dp.toPx()))
        axisTicks(period.axisStartMinute, period.axisEndMinute).forEach { tick ->
            val x = size.width * (tick - period.axisStartMinute) / axisSpan
            drawLine(grid, Offset(x, 4.dp.toPx()), Offset(x, size.height - 4.dp.toPx()), 1.dp.toPx())
        }
        val barHeight = 10.dp.toPx()
        val gap = 4.dp.toPx()
        val stackHeight = row.nights.size * barHeight + (row.nights.size - 1).coerceAtLeast(0) * gap
        val top = (size.height - stackHeight) / 2f
        row.nights.sortedBy { it.bedtimeMs }.forEachIndexed { index, night ->
            val start = SleepTimetableData.wallMinute(night.bedtimeMs, row.date, zone)
            val end = SleepTimetableData.wallMinute(night.wakeMs, row.date, zone)
            val left = size.width * ((start - period.axisStartMinute) / axisSpan).toFloat()
            val right = size.width * ((end - period.axisStartMinute) / axisSpan).toFloat()
            val x = left.coerceIn(0f, size.width)
            val width = (right.coerceIn(0f, size.width) - x).coerceAtLeast(2.dp.toPx())
            val y = top + index * (barHeight + gap)
            if (night.isManual) {
                drawRoundRect(
                    manualColor, Offset(x, y), Size(width, barHeight), CornerRadius(barHeight / 2),
                    style = Stroke(2.dp.toPx(), pathEffect = PathEffect.dashPathEffect(
                        floatArrayOf(8.dp.toPx(), 5.dp.toPx())
                    ))
                )
            } else {
                drawRoundRect(autoColor, Offset(x, y), Size(width, barHeight), CornerRadius(barHeight / 2))
            }
        }
    }
}

@Composable
private fun TimetableLegend(label: String, outlined: Boolean) {
    val color = if (outlined) MaterialTheme.colorScheme.tertiary else MaterialTheme.colorScheme.secondary
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Canvas(Modifier.size(width = 26.dp, height = 10.dp)) {
            if (outlined) drawRoundRect(color, style = Stroke(2.dp.toPx(),
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(8.dp.toPx(), 5.dp.toPx()))))
            else drawRoundRect(color)
        }
        Text(label, style = MaterialTheme.typography.labelSmall)
    }
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
fun SleepHistoryDetailSheet(
    night: SleepHistoryNight,
    zone: ZoneId,
    onDismiss: () -> Unit
) {
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ) {
        Column(
            Modifier.fillMaxWidth().fillMaxHeight(.92f).verticalScroll(rememberScrollState())
                .padding(start = 16.dp, end = 16.dp, bottom = 32.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("날짜별 상세", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    Text("현재 기기 시간대 · ${zone.id}", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                TextButton(onClick = onDismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("닫기") }
            }
            LatestSleepCard(
                nights = listOf(night), zone = zone, onConnect = {}, title = "선택한 수면"
            )
            DetailSupplement(night, zone)
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun DetailSupplement(night: SleepHistoryNight, zone: ZoneId) {
    val facts = buildList {
        night.sleepLatencyMinutes?.let { add("입면까지 ${it}분") }
        night.awakeMinutes?.let { add("중간 각성 ${it}분") }
        night.rested?.let { add("개운함 $it/5") }
        night.fatigue?.let { add(FatigueScale.displayText(it, night.fatigueScaleMax)) }
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        shape = RoundedCornerShape(18.dp)
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("기록 정보", style = MaterialTheme.typography.titleMedium)
            if (facts.isEmpty() && night.survey == null) {
                Text("추가 측정값이나 보완 설문은 없어요.", style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    facts.forEach { fact ->
                        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerHighest)) {
                            Text(fact, Modifier.padding(horizontal = 10.dp, vertical = 7.dp),
                                style = MaterialTheme.typography.labelLarge)
                        }
                    }
                }
                night.survey?.let {
                    Text("보완 설문 · ${it.summaryIn(zone, includeAlarm = false)}",
                        style = MaterialTheme.typography.bodyMedium)
                }
            }
            HorizontalDivider()
            Text(
                if (night.isManual) "직접 기록이 같은 기상일의 자동 기록보다 우선 적용된 기록입니다."
                else "기록 출처 · ${night.sourceLabel}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

private fun axisTicks(startMinute: Int, endMinute: Int): List<Int> {
    val span = (endMinute - startMinute).coerceAtLeast(1)
    val desired = ceil(span / 4.0).toInt()
    val step = listOf(60, 120, 180, 240, 360, 480, 720, 1_440).firstOrNull { it >= desired } ?: 1_440
    val first = Math.floorDiv(startMinute, step) * step
    return generateSequence(first) { it + step }
        .dropWhile { it < startMinute }
        .takeWhile { it <= endMinute }
        .toList()
        .ifEmpty { listOf(startMinute, endMinute) }
}

private fun axisLabel(minute: Int): String {
    val dayOffset = Math.floorDiv(minute, 1_440)
    val clockMinute = Math.floorMod(minute, 1_440)
    val hour = clockMinute / 60
    return when {
        dayOffset < 0 -> "전날\n%02d시".format(hour)
        dayOffset > 0 -> "다음날\n%02d시".format(hour)
        else -> "%02d시".format(hour)
    }
}

private fun timetableTime(ms: Long, zone: ZoneId): String =
    Instant.ofEpochMilli(ms).atZone(zone).format(timetableClock)

private fun timetableDurationText(minutes: Int): String = "${minutes / 60}시간" +
    (minutes % 60).takeIf { it > 0 }?.let { " ${it}분" }.orEmpty()

private fun LocalDate.coerceAtMost(other: LocalDate): LocalDate = if (isAfter(other)) other else this
