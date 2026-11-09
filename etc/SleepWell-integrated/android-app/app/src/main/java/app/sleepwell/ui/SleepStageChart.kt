package app.sleepwell.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.unit.dp
import app.sleepwell.health.EPOCH_MS
import app.sleepwell.health.EpochRow
import app.sleepwell.health.TimeFmt

/*
 * 수면 그래프(hypnogram)와 심박 그래프.
 *
 * 차트 라이브러리를 쓰지 않고 Compose Canvas 로 직접 그린다.
 * 이유: 라이브러리를 추가하면 Gradle 의존성을 손봐야 하고, 그러면 채윤이 만든 프로젝트와
 * 빌드 설정이 갈라질 위험이 있다. MVP 에서 필요한 그림은 꺾은선 두 개뿐이라 직접 그리는
 * 편이 오히려 확실하다.
 *
 * hypnogram 은 수면 연구의 표준 그림이다. 위에서 아래로 갈수록 깊은 잠이고,
 * 시간에 따라 어떤 단계를 지나갔는지를 계단 모양으로 보여준다.
 */

/** 단계별 색. 깊어질수록 진한 파랑, REM 은 눈에 띄는 보라 (관례) */
private val STAGE_COLORS = mapOf(
    0 to Color(0xFFE8853A),   // W   주황
    4 to Color(0xFF9B5DE5),   // REM 보라
    1 to Color(0xFF7FB3E8),   // N1  옅은 파랑
    2 to Color(0xFF4A80C4),   // N2
    3 to Color(0xFF1F4E8C)    // N3  진한 파랑
)

/**
 * 심박 표본 간격의 **중앙값 × 이 배수** 를 넘으면 선을 끊는다.
 *
 * 고정값(예: 5분)을 쓰면 안 되는 이유: 워치 설정에 따라 심박 측정 간격이 달라진다.
 * "연속 측정"이면 1분 간격, "10분마다"면 10분 간격으로 들어온다.
 * 5분으로 고정해두면 10분 간격 데이터에서는 **모든 간격이 기준을 넘어서**
 * 선이 한 번도 안 그려진다 (= 화면이 텅 빈다).
 * 그래서 기준을 데이터에서 직접 알아낸다. 어떤 설정이든 자동으로 맞는다.
 */
private const val HR_BRIDGE_FACTOR = 3

/**
 * 중앙값 추정의 하한/상한.
 * - 하한: 삼성이 몇 초 간격 버스트로 표본을 주면 중앙값이 너무 작아져서 전부 끊긴다.
 * - 상한: 간격이 아주 넓으면(30분 등) 3배가 90분이 되어 시계를 벗은 구간까지 이어버린다.
 */
private const val HR_BRIDGE_MIN_MS = 90_000L        // 1.5분
private const val HR_BRIDGE_MAX_MS = 40 * 60_000L   // 40분

/**
 * 표본 시각들로부터 "선을 끊을 간격"을 정한다.
 *
 * 평균이 아니라 **중앙값**을 쓴다. 시계를 벗은 큰 공백이 하나만 있어도 평균은
 * 크게 끌려올라가서, 정작 끊어야 하는 그 공백까지 이어버린다. 중앙값은 안 흔들린다.
 */
private fun hrBridgeMs(times: List<Long>): Long {
    if (times.size < 3) return HR_BRIDGE_MIN_MS
    val gaps = times.zipWithNext { a, b -> b - a }.filter { it > 0 }.sorted()
    if (gaps.isEmpty()) return HR_BRIDGE_MIN_MS
    val median = gaps[gaps.size / 2]
    return (median * HR_BRIDGE_FACTOR).coerceIn(HR_BRIDGE_MIN_MS, HR_BRIDGE_MAX_MS)
}

/** 그래프에서 위→아래 순서. hypnogram 관례상 W 가 맨 위, N3 가 맨 아래. */
private val ROW_ORDER = listOf(0, 4, 1, 2, 3)
private val ROW_LABEL = mapOf(0 to "W", 4 to "REM", 1 to "N1", 2 to "N2", 3 to "N3")

@Composable
fun SleepStageChart(
    rows: List<EpochRow>,
    modifier: Modifier = Modifier
) {
    if (rows.isEmpty()) {
        Text("표시할 수면 데이터가 없습니다.", style = MaterialTheme.typography.bodySmall)
        return
    }

    // night 이 아니라 **세션** 단위로 그린다. 같은 night 에 낮잠과 밤잠이 함께 있으면
    // night 으로 묶었을 때 사이의 9시간 공백까지 한 그래프에 들어가서, 정작 보려는
    // 수면 구조가 양쪽 끝에 눌려 보이지 않게 된다.
    val sessions = rows.groupBy { it.sessionId }.values.sortedBy { s -> s.minOf { it.timeMs } }

    Column(modifier) {
        sessions.forEach { sessionRows ->
            val sorted = sessionRows.sortedBy { it.epochIdx }
            val start = sorted.first().timeMs
            val end = sorted.last().timeMs + EPOCH_MS

            Text(
                "${TimeFmt.day(start)} ${TimeFmt.hhmm(start)} → ${TimeFmt.hhmm(end)} " +
                        "· night ${sorted.first().night}",
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(top = 12.dp, bottom = 4.dp)
            )

            Canvas(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(140.dp)
            ) { drawHypnogram(sorted, start, end) }

            val hasHr = sorted.any { it.hrMean != null }
            if (hasHr) {
                Text(
                    "심박 (bpm)",
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.padding(top = 8.dp)
                )
                Canvas(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(90.dp)
                ) { drawHeartRate(sorted, start, end) }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────
// hypnogram
// ─────────────────────────────────────────────────────────────
private fun DrawScope.drawHypnogram(rows: List<EpochRow>, startMs: Long, endMs: Long) {
    val leftPad = 34.dp.toPx()
    val bottomPad = 16.dp.toPx()
    val plotW = size.width - leftPad
    val plotH = size.height - bottomPad
    val span = (endMs - startMs).coerceAtLeast(1L).toFloat()
    val rowH = plotH / ROW_ORDER.size

    fun x(ms: Long) = leftPad + plotW * (ms - startMs) / span
    fun yOf(label: Int): Float {
        val i = ROW_ORDER.indexOf(label)
        return if (i < 0) plotH else rowH * (i + 0.5f)
    }

    // 배경 격자 + 단계 이름
    ROW_ORDER.forEachIndexed { i, label ->
        val y = rowH * (i + 0.5f)
        drawLine(Color(0x14000000), Offset(leftPad, y), Offset(size.width, y), 1f)
        drawIntoCanvas { c ->
            c.nativeCanvas.drawText(
                ROW_LABEL[label] ?: "",
                4f, y + 4.dp.toPx(),
                labelPaint(density = density)
            )
        }
    }

    // 계단 선 — UNKNOWN(-1)은 선을 잇지 않고 끊는다.
    // 없는 정보를 직선으로 이어버리면 "그 시간에도 측정됐다"는 착각을 준다.
    val path = Path()
    var open = false
    var prevY = 0f
    rows.forEach { r ->
        if (r.stageLabel !in 0..4) {
            open = false
            return@forEach
        }
        val xs = x(r.timeMs)
        val xe = x(r.timeMs + EPOCH_MS)
        val y = yOf(r.stageLabel)
        if (!open) {
            path.moveTo(xs, y)
            open = true
        } else if (y != prevY) {
            path.lineTo(xs, prevY)
            path.lineTo(xs, y)      // 수직 전환 → 계단 모양
        }
        path.lineTo(xe, y)
        prevY = y
    }
    drawPath(path, Color(0xFF33507A), style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round))

    // 단계별 색 띠 — 계단선만으로는 REM 이 어디인지 한눈에 안 보인다
    rows.forEach { r ->
        val c = STAGE_COLORS[r.stageLabel] ?: return@forEach
        drawRect(
            color = c.copy(alpha = 0.75f),
            topLeft = Offset(x(r.timeMs), plotH + 2.dp.toPx()),
            size = androidx.compose.ui.geometry.Size(
                width = (x(r.timeMs + EPOCH_MS) - x(r.timeMs)).coerceAtLeast(1f),
                height = 6.dp.toPx()
            )
        )
    }

    drawHourTicks(startMs, endMs, leftPad, plotW, plotH)
}

// ─────────────────────────────────────────────────────────────
// 심박
// ─────────────────────────────────────────────────────────────
private fun DrawScope.drawHeartRate(rows: List<EpochRow>, startMs: Long, endMs: Long) {
    val leftPad = 34.dp.toPx()
    val bottomPad = 16.dp.toPx()
    val plotW = size.width - leftPad
    val plotH = size.height - bottomPad
    val span = (endMs - startMs).coerceAtLeast(1L).toFloat()

    val values = rows.mapNotNull { it.hrMean }
    if (values.isEmpty()) return
    val lo = (values.min() - 3f)
    val hi = (values.max() + 3f)
    val range = (hi - lo).coerceAtLeast(1f)

    fun x(ms: Long) = leftPad + plotW * (ms - startMs) / span
    fun y(v: Float) = plotH - plotH * (v - lo) / range

    // 최소/최대 눈금
    listOf(hi, (hi + lo) / 2f, lo).forEach { v ->
        val yy = y(v)
        drawLine(Color(0x14000000), Offset(leftPad, yy), Offset(size.width, yy), 1f)
        drawIntoCanvas { c ->
            c.nativeCanvas.drawText(
                v.toInt().toString(), 2f, yy + 4.dp.toPx(), labelPaint(density = density)
            )
        }
    }

    // 결측 구간에서 선을 끊는다 (시계를 벗은 시간을 직선으로 메우지 않는다).
    //
    // ★ 주의: "hrMean 이 null 이면 끊는다" 로 만들면 선이 아예 안 그려진다.
    //   심박은 보통 60초 간격으로 들어오는데 격자는 30초라, null 이 한 칸씩 번갈아 낀다.
    //   그래서 매 칸마다 선이 끊겨 moveTo 만 반복되고 lineTo 가 한 번도 실행되지 않는다.
    //   (에뮬레이터로 실제로 띄워보고 발견했다. 컴파일로는 안 잡히는 버그다.)
    //
    //   구분해야 하는 건 두 가지다.
    //     · 격자가 촘촘해서 생긴 빈칸  → 측정은 됐다. 이어야 한다.
    //     · 시계를 벗어서 생긴 빈칸    → 측정이 없다. 끊어야 한다.
    //   기준을 시간으로 잡는다. 표본 간격이 bridgeMs 안이면 이어 붙이고,
    //   그보다 벌어지면 실제 결측으로 보고 끊는다.
    //
    // ★ 그 bridgeMs 를 고정값(5분)으로 두지 않는 이유 (2026-09-18 수정):
    //   워치 설정에 따라 심박 측정 간격이 달라진다 — "연속"이면 1분, "10분마다"면 10분.
    //   5분으로 고정하면 10분 간격 데이터에서 모든 간격이 기준을 넘어, 위에 적은
    //   버그가 **똑같은 증상으로 재발한다**(선이 한 번도 안 그려짐).
    //   그래서 기준을 데이터에서 직접 재고, 설정을 몰라도 맞게 만든다.
    val pts = rows.mapNotNull { r -> r.hrMean?.let { r.timeMs to it } }
    val bridgeMs = hrBridgeMs(pts.map { it.first })
    val path = Path()
    var prevMs = Long.MIN_VALUE
    pts.forEach { (ms, v) ->
        val px = x(ms)
        val py = y(v)
        if (prevMs == Long.MIN_VALUE || ms - prevMs > bridgeMs) path.moveTo(px, py)
        else path.lineTo(px, py)
        prevMs = ms
    }
    drawPath(path, Color(0xFFD1495B), style = Stroke(width = 1.6.dp.toPx(), cap = StrokeCap.Round))

    // 표본 자체도 점으로 찍는다. 앞뒤가 모두 결측인 외톨이 표본은 선으로는 보이지 않는다.
    val dot = 1.4.dp.toPx()
    pts.forEach { (ms, v) -> drawCircle(Color(0xFFD1495B), dot, Offset(x(ms), y(v))) }

    drawHourTicks(startMs, endMs, leftPad, plotW, plotH)
}

// ─────────────────────────────────────────────────────────────
// 공통: 정시마다 시각 눈금
// ─────────────────────────────────────────────────────────────
private fun DrawScope.drawHourTicks(
    startMs: Long,
    endMs: Long,
    leftPad: Float,
    plotW: Float,
    plotH: Float
) {
    val hour = 3_600_000L
    val span = (endMs - startMs).coerceAtLeast(1L).toFloat()
    var t = ((startMs + hour - 1) / hour) * hour     // 첫 정시로 올림
    while (t < endMs) {
        val px = leftPad + plotW * (t - startMs) / span
        drawLine(Color(0x1A000000), Offset(px, 0f), Offset(px, plotH), 1f)
        drawIntoCanvas { c ->
            c.nativeCanvas.drawText(
                TimeFmt.hhmm(t), px - 12.dp.toPx(), size.height - 2.dp.toPx(),
                labelPaint(density = density)
            )
        }
        t += hour
    }
}

/** nativeCanvas 용 글자 설정. Compose 밖 API 라 여기서만 쓴다. */
private fun labelPaint(density: Float) = android.graphics.Paint().apply {
    color = android.graphics.Color.parseColor("#666666")
    textSize = 9f * density
    isAntiAlias = true
}

/** DrawScope 에서 native canvas 를 꺼내는 헬퍼 (import 를 한 곳으로 모으기 위함) */
private inline fun DrawScope.drawIntoCanvas(block: (androidx.compose.ui.graphics.Canvas) -> Unit) {
    block(drawContext.canvas)
}
