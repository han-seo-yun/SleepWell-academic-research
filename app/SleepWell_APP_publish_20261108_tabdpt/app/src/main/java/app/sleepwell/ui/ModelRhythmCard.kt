package app.sleepwell.ui

import android.graphics.Paint
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.AnalysisDisplay
import app.sleepwell.analysis.ChartPoint
import app.sleepwell.analysis.PlanKind
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.math.abs

private val RhythmS = Color(0xFF3167B1)
private val RhythmC = Color(0xFFB85210)
private val RhythmP = Color(0xFF7047B5)
private val ObservedSleep = Color(0xFF7CB7CF)
private val ManualSleep = Color(0xFFAA8BD1)
private val PredictedSleep = Color(0xFF687386)
private val RecommendedSleep = Color(0xFF17794A)

private const val HOUR_MS = 60 * 60 * 1000L
private val ModelChartLeftPadding = 40.dp
private val ModelChartRightPadding = 4.dp

internal data class ModelChartProjection(
    val startMs: Long,
    val endMs: Long,
    val low: Double,
    val high: Double,
    val points: List<ChartPoint>
)

internal fun modelChartProjection(
    display: AnalysisDisplay,
    futurePoints: List<ChartPoint> = display.future
): ModelChartProjection? {
    val points = (display.history + futurePoints).sortedBy { it.timeMs }
    val timeCandidates = buildList {
        addAll(points.map { it.timeMs })
        add(display.referenceMs)
        display.sleepBands.forEach { add(it.startMs); add(it.endMs) }
        display.manualSleep?.let { add(it.startMs); add(it.endMs) }
        display.planBands.forEach { add(it.startMs); add(it.endMs) }
    }
    if (timeCandidates.isEmpty()) return null
    val fallbackStart = timeCandidates.minOrNull() ?: return null
    val fallbackEnd = timeCandidates.maxOrNull() ?: return null
    val start = display.graphStartMs.takeIf { it > 0 } ?: fallbackStart
    val end = display.graphEndMs.takeIf { it > start } ?: fallbackEnd.coerceAtLeast(start + HOUR_MS)
    val values = points.flatMap { listOf(it.s, it.c, it.propensity) }
    val low = (values.minOrNull() ?: 0.0) - 0.05
    val high = ((values.maxOrNull() ?: 1.0) + 0.05).coerceAtLeast(low + 0.1)
    return ModelChartProjection(start, end, low, high, points)
}

internal fun nearestModelPoint(
    points: List<ChartPoint>,
    targetMs: Long,
    startMs: Long,
    endMs: Long
): ChartPoint? = points.asSequence()
    .filter { it.timeMs in startMs..endMs }
    .minByOrNull { abs(it.timeMs - targetMs) }

/**
 * The model's two-process rhythm is shown independently from measured sleep stages.
 * Solid lines are calculated through the cutoff. Dashed lines can show the original
 * awake-continuation forecast or a display-only scenario that applies the predicted sleep band.
 */
@Composable
fun ModelRhythmCard(
    display: AnalysisDisplay?,
    nowMs: Long,
    modifier: Modifier = Modifier
) {
    val zone = remember(UiZone.current.zoneId) { ZoneId.of(UiZone.current.zoneId) }
    val sleepScenario = remember(display) {
        display?.let { predictedSleepScenario(it.future, it.planBands, it.referenceMs) }
    }
    // Keep the user's comparison choice while the live display is recalculated each minute.
    var showSleepScenario by remember { mutableStateOf(false) }
    val futurePoints = if (showSleepScenario) sleepScenario ?: display?.future.orEmpty()
        else display?.future.orEmpty()
    val projection = remember(display, futurePoints) {
        display?.let { modelChartProjection(it, futurePoints) }
    }
    var selected by remember(display) { mutableStateOf<ChartPoint?>(null) }
    val note = MaterialTheme.colorScheme.onSurfaceVariant
    val outline = MaterialTheme.colorScheme.outlineVariant
    val semanticSummary = remember(display, projection, selected, zone, showSleepScenario, sleepScenario) {
        buildString {
            append("모델 리듬 그래프. 수면 압력, 일주기 리듬, 잠들기 쉬운 정도를 모델 추정 지수로 표시합니다. ")
            if (projection == null || projection.points.isEmpty()) append("표시할 모델 계산값이 없습니다.")
            else {
                append(if (showSleepScenario && sleepScenario != null)
                    "실선은 계산된 과거, 점선은 예상 수면 구간 내내 잠든다고 가정한 조건부 시나리오입니다. "
                else "실선은 계산된 과거, 점선은 계속 각성 상태를 가정한 비교 계산입니다. ")
                selected?.let { append(modelPointText(it, zone)) }
            }
        }
    }

    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
        border = BorderStroke(1.dp, outline)
    ) {
        Column(
            Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text("수면 리듬 · 모델 추정", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            }

            RhythmLegend("수면 압력 S", RhythmS, solid = true)
            RhythmLegend("일주기 리듬 C", RhythmC, solid = true)
            RhythmLegend("잠들기 쉬운 정도 S+C", RhythmP, solid = true)
            if (sleepScenario != null) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(
                        selected = showSleepScenario,
                        onClick = {
                            showSleepScenario = true
                            selected = null
                        },
                        label = { Text("예상 수면 가정") },
                        modifier = Modifier.weight(1f)
                    )
                    FilterChip(
                        selected = !showSleepScenario,
                        onClick = {
                            showSleepScenario = false
                            selected = null
                        },
                        label = { Text("계속 각성 비교") },
                        modifier = Modifier.weight(1f)
                    )
                }
            }
            RhythmLegend(
                if (showSleepScenario && sleepScenario != null)
                    "점선 · 예상 구간 내내 잠든 경우"
                else "점선 · 계속 각성 상태를 가정한 비교 계산",
                note,
                solid = false
            )

            if (projection == null || projection.points.isEmpty()) {
                EmptyRhythmScaffold()
                Text(
                    "수면 기록을 불러오고 분석이 완료되면 과거 계산과 다음 구간 예측을 여기에 표시해요.",
                    style = MaterialTheme.typography.bodySmall,
                    color = note
                )
            } else {
                val interactive = Modifier
                    .fillMaxWidth()
                    .height(220.dp)
                    .semantics { contentDescription = semanticSummary }
                    .pointerInput(projection) {
                        detectTapGestures { offset ->
                            selected = selectedPointAt(
                                offset.x, size.width.toFloat(),
                                ModelChartLeftPadding.toPx(), ModelChartRightPadding.toPx(), projection
                            )
                        }
                    }
                    .pointerInput(projection) {
                        detectHorizontalDragGestures(
                            onDragStart = { offset ->
                                selected = selectedPointAt(
                                    offset.x, size.width.toFloat(),
                                    ModelChartLeftPadding.toPx(), ModelChartRightPadding.toPx(), projection
                                )
                            },
                            onHorizontalDrag = { change, _ ->
                                selected = selectedPointAt(
                                    change.position.x, size.width.toFloat(),
                                    ModelChartLeftPadding.toPx(), ModelChartRightPadding.toPx(), projection
                                )
                            }
                        )
                    }
                val axis = MaterialTheme.colorScheme.onSurfaceVariant
                val grid = MaterialTheme.colorScheme.outlineVariant
                val surface = MaterialTheme.colorScheme.surface
                Canvas(interactive) {
                    drawModelRhythm(
                        display = requireNotNull(display),
                        futurePoints = futurePoints,
                        projection = projection,
                        nowMs = nowMs,
                        selected = selected,
                        zone = zone,
                        axisColor = axis,
                        gridColor = grid,
                        surfaceColor = surface
                    )
                }
                Text(
                    selected?.let { modelPointText(it, zone) }
                        ?: "그래프를 탭하거나 좌우로 밀어 시점별 값을 확인하세요.",
                    style = MaterialTheme.typography.bodySmall,
                    color = if (selected == null) note else MaterialTheme.colorScheme.onSurface
                )
            }

            display?.let { d ->
                if (d.sleepBands.isNotEmpty()) {
                    RhythmLegend("기록된 수면 구간", ObservedSleep, solid = true, band = true)
                }
                if (d.manualSleep != null) {
                    RhythmLegend("직접 보완한 수면 구간", ManualSleep, solid = true, band = true)
                }
                val planTime = DateTimeFormatter.ofPattern("M/d HH:mm")
                d.planBands.forEach { plan ->
                    val recommended = plan.kind == PlanKind.RECOMMENDED
                    val label = if (recommended) "권장 수면 구간" else "모델이 예상한 수면 구간"
                    val start = planTime.format(Instant.ofEpochMilli(plan.startMs).atZone(zone))
                    val end = planTime.format(Instant.ofEpochMilli(plan.endMs).atZone(zone))
                    RhythmLegend(
                        "$label\n$start ~ $end",
                        if (recommended) RecommendedSleep else PredictedSleep,
                        solid = true,
                        band = true
                    )
                }
                if (d.gapBands.isNotEmpty()) Text(
                    "빗금 구간은 수면 기록이 없어 깨어 있던 것으로 계산됐습니다. 수면 압력이 실제보다 높을 수 있어요.",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error
                )
            }
        }
    }
}

@Composable
private fun RhythmLegend(label: String, color: Color, solid: Boolean, band: Boolean = false) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Canvas(Modifier.height(12.dp).width(32.dp)) {
            drawLine(
                color = color,
                start = Offset(0f, size.height / 2),
                end = Offset(size.width, size.height / 2),
                strokeWidth = (if (band) 5.dp else 2.dp).toPx(),
                cap = StrokeCap.Round,
                pathEffect = if (solid) null else PathEffect.dashPathEffect(floatArrayOf(8f, 6f))
            )
        }
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurface)
    }
}

@Composable
private fun EmptyRhythmScaffold() {
    val grid = MaterialTheme.colorScheme.outlineVariant
    val axis = MaterialTheme.colorScheme.onSurfaceVariant
    Canvas(
        Modifier.fillMaxWidth().height(150.dp).semantics {
            contentDescription = "모델 계산값을 기다리는 빈 수면 리듬 그래프"
        }
    ) {
        val left = 36.dp.toPx()
        val bottom = size.height - 24.dp.toPx()
        repeat(3) { index ->
            val y = 16.dp.toPx() + index * (bottom - 16.dp.toPx()) / 2f
            drawLine(grid, Offset(left, y), Offset(size.width, y), 1f)
        }
        drawLine(axis, Offset(left, bottom), Offset(size.width, bottom), 1.dp.toPx())
        drawLabel("모델 값 없음", size.width / 2, size.height / 2, axis, Paint.Align.CENTER, 12f)
        drawLabel("시간", size.width, size.height - 2.dp.toPx(), axis, Paint.Align.RIGHT, 10f)
    }
}

internal fun selectedPointAt(
    x: Float,
    width: Float,
    leftPaddingPx: Float,
    rightPaddingPx: Float,
    projection: ModelChartProjection
): ChartPoint? {
    if (width <= 0f) return null
    val (left, right) = modelChartHorizontalBounds(width, leftPaddingPx, rightPaddingPx)
    val fraction = ((x - left) / (right - left)).coerceIn(0f, 1f)
    val target = projection.startMs + ((projection.endMs - projection.startMs) * fraction).toLong()
    return nearestModelPoint(projection.points, target, projection.startMs, projection.endMs)
}

internal fun modelChartHorizontalBounds(
    widthPx: Float,
    leftPaddingPx: Float,
    rightPaddingPx: Float
): Pair<Float, Float> {
    val left = leftPaddingPx.coerceAtLeast(0f)
    val right = (widthPx - rightPaddingPx.coerceAtLeast(0f)).coerceAtLeast(left + 1f)
    return left to right
}

private fun DrawScope.drawModelRhythm(
    display: AnalysisDisplay,
    futurePoints: List<ChartPoint>,
    projection: ModelChartProjection,
    nowMs: Long,
    selected: ChartPoint?,
    zone: ZoneId,
    axisColor: Color,
    gridColor: Color,
    surfaceColor: Color
) {
    val (left, right) = modelChartHorizontalBounds(
        size.width,
        ModelChartLeftPadding.toPx(),
        ModelChartRightPadding.toPx()
    )
    val top = 25.dp.toPx()
    val bottom = size.height - 28.dp.toPx()
    val plotW = (right - left).coerceAtLeast(1f)
    val plotH = (bottom - top).coerceAtLeast(1f)
    val timeSpan = (projection.endMs - projection.startMs).coerceAtLeast(1L).toFloat()
    val valueSpan = (projection.high - projection.low).coerceAtLeast(0.1)
    fun x(ms: Long) = left + plotW * (ms - projection.startMs) / timeSpan
    fun y(value: Double) = bottom - plotH * ((value - projection.low) / valueSpan).toFloat()

    repeat(3) { index ->
        val value = projection.low + valueSpan * index / 2.0
        val yy = y(value)
        drawLine(gridColor.copy(alpha = 0.7f), Offset(left, yy), Offset(right, yy), 1f)
        drawLabel(formatModelValue(value), left - 4.dp.toPx(), yy + 4.dp.toPx(), axisColor, Paint.Align.RIGHT, 9f)
    }

    val tickStep = if (projection.endMs - projection.startMs > 30 * HOUR_MS) 6 * HOUR_MS else 3 * HOUR_MS
    var tick = ((projection.startMs / tickStep) + 1) * tickStep
    val timeFormat = DateTimeFormatter.ofPattern("HH:mm")
    val dateFormat = DateTimeFormatter.ofPattern("M/d")
    while (tick < projection.endMs) {
        val px = x(tick)
        val local = Instant.ofEpochMilli(tick).atZone(zone)
        drawLine(gridColor.copy(alpha = 0.45f), Offset(px, top), Offset(px, bottom), 1f)
        val label = if (local.hour == 0) dateFormat.format(local) else timeFormat.format(local)
        drawLabel(label, px, size.height - 7.dp.toPx(), axisColor, Paint.Align.CENTER, 9f)
        tick += tickStep
    }

    fun band(startMs: Long, endMs: Long, color: Color, bandTop: Float = top, bandBottom: Float = bottom) {
        val xs = x(startMs.coerceIn(projection.startMs, projection.endMs))
        val xe = x(endMs.coerceIn(projection.startMs, projection.endMs))
        if (xe > xs) drawRect(color, Offset(xs, bandTop), Size(xe - xs, bandBottom - bandTop))
    }
    display.sleepBands.forEach { band(it.startMs, it.endMs, ObservedSleep.copy(alpha = 0.20f)) }
    display.manualSleep?.let { band(it.startMs, it.endMs, ManualSleep.copy(alpha = 0.22f)) }
    display.gapBands.forEach { gap ->
        val xs = x(gap.startMs.coerceIn(projection.startMs, projection.endMs))
        val xe = x(gap.endMs.coerceIn(projection.startMs, projection.endMs))
        if (xe > xs) clipRect(xs, top, xe, bottom) {
            var stripe = xs - plotH
            while (stripe < xe) {
                drawLine(axisColor.copy(alpha = 0.22f), Offset(stripe, bottom), Offset(stripe + plotH, top), 1.dp.toPx())
                stripe += 8.dp.toPx()
            }
        }
    }

    display.planBands.forEach { band ->
        val xs = x(band.startMs.coerceIn(projection.startMs, projection.endMs))
        val xe = x(band.endMs.coerceIn(projection.startMs, projection.endMs))
        if (xe > xs) {
            val color = if (band.kind == PlanKind.RECOMMENDED) RecommendedSleep else PredictedSleep
            val yy = top + if (band.kind == PlanKind.RECOMMENDED) 9.dp.toPx() else 1.dp.toPx()
            drawRoundRect(color, Offset(xs, yy), Size(xe - xs, 5.dp.toPx()), CornerRadius(4.dp.toPx()))
        }
    }

    fun curve(points: List<ChartPoint>, color: Color, value: (ChartPoint) -> Double, width: Float, dashed: Boolean) {
        val visible = points.filter { it.timeMs in projection.startMs..projection.endMs }
        if (visible.isEmpty()) return
        val path = Path()
        visible.forEachIndexed { index, point ->
            if (index == 0) path.moveTo(x(point.timeMs), y(value(point))) else path.lineTo(x(point.timeMs), y(value(point)))
        }
        drawPath(
            path,
            color,
            style = Stroke(
                width = width,
                cap = StrokeCap.Round,
                pathEffect = if (dashed) PathEffect.dashPathEffect(floatArrayOf(10f, 8f)) else null
            )
        )
    }
    curve(display.history, RhythmS, { it.s }, 2.dp.toPx(), false)
    curve(display.history, RhythmC, { it.c }, 2.dp.toPx(), false)
    curve(display.history, RhythmP, { it.propensity }, 2.6.dp.toPx(), false)
    curve(futurePoints, RhythmS.copy(alpha = 0.82f), { it.s }, 2.dp.toPx(), true)
    curve(futurePoints, RhythmC.copy(alpha = 0.82f), { it.c }, 2.dp.toPx(), true)
    curve(futurePoints, RhythmP.copy(alpha = 0.82f), { it.propensity }, 2.6.dp.toPx(), true)

    if (display.referenceMs in projection.startMs..projection.endMs) {
        val px = x(display.referenceMs)
        drawLine(axisColor.copy(alpha = 0.75f), Offset(px, top), Offset(px, bottom), 1.5.dp.toPx())
        drawLabel(if (display.basisIsWake) "기상·계산 기준" else "계산 기준", px, 12.dp.toPx(), axisColor, Paint.Align.CENTER, 9f)
    }
    if (nowMs in projection.startMs..projection.endMs && abs(nowMs - display.referenceMs) > 10 * 60_000L) {
        val px = x(nowMs)
        drawLine(RecommendedSleep.copy(alpha = 0.85f), Offset(px, top), Offset(px, bottom), 1.dp.toPx(), pathEffect = PathEffect.dashPathEffect(floatArrayOf(5f, 5f)))
        drawLabel("현재", px, bottom - 4.dp.toPx(), RecommendedSleep, Paint.Align.CENTER, 9f)
    }

    selected?.takeIf { it.timeMs in projection.startMs..projection.endMs }?.let { point ->
        val px = x(point.timeMs)
        drawLine(axisColor.copy(alpha = 0.55f), Offset(px, top), Offset(px, bottom), 1.dp.toPx())
        listOf(point.s to RhythmS, point.c to RhythmC, point.propensity to RhythmP).forEach { (value, color) ->
            drawCircle(surfaceColor, 5.dp.toPx(), Offset(px, y(value)))
            drawCircle(color, 3.2.dp.toPx(), Offset(px, y(value)))
        }
    }
}

private fun modelPointText(point: ChartPoint, zone: ZoneId): String {
    val shown = DateTimeFormatter.ofPattern("M월 d일 HH:mm").format(Instant.ofEpochMilli(point.timeMs).atZone(zone))
    return "$shown · 모델 추정 지수 S ${formatModelValue(point.s)} · C ${formatModelValue(point.c)} · S+C ${formatModelValue(point.propensity)}"
}

private fun formatModelValue(value: Double): String = String.format(java.util.Locale.US, "%.2f", value)

private fun DrawScope.drawLabel(
    text: String,
    x: Float,
    y: Float,
    color: Color,
    align: Paint.Align,
    sizeDp: Float
) {
    drawContext.canvas.nativeCanvas.drawText(text, x, y, Paint().apply {
        this.color = android.graphics.Color.argb(
            (color.alpha * 255).toInt(),
            (color.red * 255).toInt(),
            (color.green * 255).toInt(),
            (color.blue * 255).toInt()
        )
        // Canvas axis labels stay in dp so font scaling cannot change plot geometry.
        // The same information is also exposed by scalable Compose text/semantics.
        textSize = sizeDp * density
        textAlign = align
        isAntiAlias = true
    })
}
