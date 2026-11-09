package app.sleepwell.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalDensity
import app.sleepwell.analysis.ScheduleRecommendation
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.sleepwell.analysis.Schedule
import app.sleepwell.analysis.ScheduleLevel
import app.sleepwell.analysis.ScheduleTimes

/** v7 hierarchy: actionable recommendation first; prediction and research detail on demand. */
@Composable
fun ScheduleCard(s: Schedule, onAskTargetWake: () -> Unit) {
    var detailsExpanded by rememberSaveable(s.basisMs, s.isPast) { mutableStateOf(s.isPast) }
    val line = MaterialTheme.colorScheme.outlineVariant
    val note = MaterialTheme.colorScheme.onSurfaceVariant
    val predictionBackground = MaterialTheme.colorScheme.surfaceContainer
    val predictionText = MaterialTheme.colorScheme.onSurfaceVariant
    val recommendationBackground = MaterialTheme.colorScheme.primaryContainer
    val recommendationText = MaterialTheme.colorScheme.onPrimaryContainer

    Card(modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, line)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(if (s.isPast) s.title else "오늘의 수면 계획", style = MaterialTheme.typography.titleLarge)
            Text(s.basisText, style = MaterialTheme.typography.labelSmall, color = note)
            if (s.isPast) {
                PastComparison(s, predictionBackground, line, predictionText, note)
                Text("권장 시각은 가장 최근 날짜에서 확인할 수 있어요.",
                    style = MaterialTheme.typography.labelSmall, color = note)
            } else {
                RecommendationFirst(s, recommendationBackground, line, recommendationText, onAskTargetWake)
            }
            TextButton(onClick = { detailsExpanded = !detailsExpanded }, modifier = Modifier.fillMaxWidth()) {
                Text(if (detailsExpanded) "추천 이유 접기" else "이 시간이 권장된 이유 보기")
            }
            if (detailsExpanded) {
                StatusLine(s)
                if (!s.isPast) {
                    Section(predictionBackground, line) {
                        Text("모델 예상", style = MaterialTheme.typography.labelLarge,
                            color = predictionText, fontWeight = FontWeight.Bold)
                        if (s.model != null) TimesRow(s.model, MaterialTheme.colorScheme.onSurface, note)
                        else Text(s.modelNote, style = MaterialTheme.typography.bodySmall,
                            color = note, modifier = Modifier.padding(top = 4.dp))
                    }
                }
                Text("내 평소 패턴 · " + (s.habit?.let {
                    "${it.bedText} 취침 → ${it.wakeText} 기상 (${s.habitNote})"
                } ?: s.habitNote), style = MaterialTheme.typography.bodySmall, color = predictionText)
                Text(s.note, style = MaterialTheme.typography.labelSmall, color = note)
            }
        }
    }
}

@Composable
private fun RecommendationFirst(s: Schedule, background: Color, line: Color, textColor: Color,
    onAskTargetWake: () -> Unit) {
    Section(background, line) {
        val rec = s.recommended
        when {
            rec != null -> {
                Text("오늘의 권장 수면", style = MaterialTheme.typography.labelLarge,
                    color = textColor, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(6.dp))
                RecommendationTimes(rec, textColor)
                rec.sleepText?.let {
                    Spacer(Modifier.height(4.dp))
                    Text(it, style = MaterialTheme.typography.bodySmall, color = textColor)
                }
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = onAskTargetWake, modifier = Modifier.fillMaxWidth()) {
                    Text("목표 기상 시각 수정")
                }
            }
            s.needsTargetWake -> {
                Text("오늘의 권장 수면", style = MaterialTheme.typography.labelLarge,
                    color = textColor, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(4.dp))
                Text("일어나야 할 시각을 알려 주면 권장 취침 시각을 계산해 드려요.",
                    style = MaterialTheme.typography.bodyMedium, color = textColor)
                Spacer(Modifier.height(8.dp))
                Button(onClick = onAskTargetWake, modifier = Modifier.fillMaxWidth(),
                    colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary,
                        contentColor = MaterialTheme.colorScheme.onPrimary)) { Text("목표 기상 시각 입력") }
            }
            else -> {
                Text("오늘의 권장 수면", style = MaterialTheme.typography.labelLarge,
                    color = textColor, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(4.dp))
                Text(s.unavailableText.orEmpty(), style = MaterialTheme.typography.bodySmall, color = textColor)
            }
        }
    }
}

@Composable
private fun PastComparison(s: Schedule, background: Color, line: Color, textColor: Color, note: Color) {
    Section(background, line) {
        Text("예상과 실제", style = MaterialTheme.typography.labelLarge,
            color = textColor, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(6.dp))
        Text(s.modelLabel, style = MaterialTheme.typography.labelMedium, color = note)
        if (s.model != null) TimesRow(s.model, MaterialTheme.colorScheme.onSurface, note)
        if (s.modelNote.isNotBlank()) Text(s.modelNote, style = MaterialTheme.typography.bodySmall, color = note)
        Spacer(Modifier.height(10.dp))
        Text("실제", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
        s.actual?.let { TimesRow(it, MaterialTheme.colorScheme.primary, note) }
        s.actualNote?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = note) }
        s.diffText?.let {
            Spacer(Modifier.height(8.dp))
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
                Text("차이 · $it", style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.Medium, modifier = Modifier.padding(8.dp))
            }
        }
    }
}

@Composable
private fun StatusLine(s: Schedule) {
    val (bg, fg, bar) = when (s.status.level) {
        ScheduleLevel.LOW -> Triple(MaterialTheme.colorScheme.errorContainer,
            MaterialTheme.colorScheme.onErrorContainer, MaterialTheme.colorScheme.error)
        ScheduleLevel.WARN -> Triple(MaterialTheme.colorScheme.tertiaryContainer,
            MaterialTheme.colorScheme.onTertiaryContainer, MaterialTheme.colorScheme.tertiary)
        ScheduleLevel.INFO -> Triple(MaterialTheme.colorScheme.surfaceContainerHigh,
            MaterialTheme.colorScheme.onSurfaceVariant, MaterialTheme.colorScheme.primary)
    }
    Card(colors = CardDefaults.cardColors(containerColor = bg), shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(10.dp)) {
            Text(s.status.text, style = MaterialTheme.typography.bodySmall, color = fg,
                fontWeight = FontWeight.Medium)
            s.notices.forEach { Text("· $it", style = MaterialTheme.typography.labelSmall, color = fg) }
            s.status.progress?.let { p ->
                Spacer(Modifier.height(6.dp))
                LinearProgressIndicator(progress = { p.coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth().height(5.dp), color = bar,
                    trackColor = bar.copy(alpha = 0.2f))
            }
        }
    }
}

@Composable
private fun Section(background: Color, line: Color, content: @Composable () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = background), shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, line), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) { content() }
    }
}

@Composable
private fun TimesRow(t: ScheduleTimes, valueColor: Color, noteColor: Color) {
    Row(Modifier.fillMaxWidth().padding(top = 4.dp), verticalAlignment = Alignment.Bottom) {
        Column(Modifier.weight(1f)) {
            Text("취침", style = MaterialTheme.typography.labelSmall, color = noteColor)
            Text(t.bedText, fontSize = 17.sp, fontWeight = FontWeight.Bold, color = valueColor)
        }
        Text("→", fontSize = 17.sp, color = noteColor, modifier = Modifier.padding(horizontal = 6.dp))
        Column(Modifier.weight(1f)) {
            Text("기상", style = MaterialTheme.typography.labelSmall, color = noteColor)
            Text(t.wakeText, fontSize = 17.sp, fontWeight = FontWeight.Bold, color = valueColor)
        }
    }
}

@Composable
private fun RecommendationTimes(rec: ScheduleRecommendation, color: Color) {
    val zone = ZoneId.of(UiZone.current.zoneId)
    @Composable fun value(label: String, ms: Long?, fallback: String, modifier: Modifier = Modifier) {
        Column(modifier) {
            Text(label, style = MaterialTheme.typography.labelMedium, color = color)
            Text(ms?.let { Instant.ofEpochMilli(it).atZone(zone).format(DateTimeFormatter.ofPattern("HH:mm")) } ?: fallback,
                style = MaterialTheme.typography.headlineMedium, color = color)
            ms?.let { Text(Instant.ofEpochMilli(it).atZone(zone).format(DateTimeFormatter.ofPattern("M월 d일 E요일", java.util.Locale.KOREAN)),
                style = MaterialTheme.typography.labelSmall, color = color) }
        }
    }
    if (LocalDensity.current.fontScale > 1.3f) Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        value("권장 취침", rec.bedMs, rec.bedText)
        value("목표 기상", rec.targetWakeMs, rec.targetWakeText)
    } else Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        value("권장 취침", rec.bedMs, rec.bedText, Modifier.weight(1f))
        value("목표 기상", rec.targetWakeMs, rec.targetWakeText, Modifier.weight(1f))
    }
}
