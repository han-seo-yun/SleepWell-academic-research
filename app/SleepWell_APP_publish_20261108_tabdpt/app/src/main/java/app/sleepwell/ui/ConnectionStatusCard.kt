package app.sleepwell.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.BackgroundSleepSync
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

internal enum class ConnectionVisualState { CHECKING, DISCONNECTED, SYNCING, ERROR, EMPTY, STALE, READY }

internal fun connectionVisualState(connected: Boolean?, busy: Boolean, failed: Boolean,
    count: Int, lastWake: Long?, nowMs: Long): ConnectionVisualState = when {
    busy -> ConnectionVisualState.SYNCING
    failed -> ConnectionVisualState.ERROR
    connected == null -> ConnectionVisualState.CHECKING
    !connected -> ConnectionVisualState.DISCONNECTED
    count == 0 -> ConnectionVisualState.EMPTY
    lastWake != null && nowMs - lastWake > 3 * 86_400_000L -> ConnectionVisualState.STALE
    else -> ConnectionVisualState.READY
}

@Composable
fun ConnectionStatusCard(connected: Boolean?, busy: Boolean, failed: Boolean, recordCount: Int,
    lastWakeMs: Long?, lastCheckMs: Long?, nowMs: Long, zone: ZoneId,
    background: BackgroundSleepSync.State?, onOpen: () -> Unit) {
    val state = connectionVisualState(connected, busy, failed, recordCount, lastWakeMs, nowMs)
    val title = when (state) {
        ConnectionVisualState.CHECKING -> "연결 상태 확인 중"
        ConnectionVisualState.DISCONNECTED -> "삼성 헬스 연결이 필요해요"
        ConnectionVisualState.SYNCING -> "새 수면 기록 확인 중…"
        ConnectionVisualState.ERROR -> "동기화를 확인해 주세요"
        ConnectionVisualState.EMPTY -> "연결됨 · 주 수면 기록 기다리는 중"
        ConnectionVisualState.STALE -> "연결됨 · 최근 기록을 확인해 주세요"
        ConnectionVisualState.READY -> "연결됨 · 주 수면 ${recordCount}일 보관"
    }
    Surface(onClick = onOpen, shape = MaterialTheme.shapes.medium,
        color = if (state == ConnectionVisualState.ERROR) MaterialTheme.colorScheme.errorContainer
            else MaterialTheme.colorScheme.surfaceContainer,
        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
        Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, style = MaterialTheme.typography.labelLarge)
            val checked = listOfNotNull(lastCheckMs, background?.lastSuccessMs?.takeIf { it > 0 }).maxOrNull()
            Text(checked?.let {
                "마지막 확인 ${Instant.ofEpochMilli(it).atZone(zone).format(DateTimeFormatter.ofPattern("M/d HH:mm"))} · 연결 관리 ›"
            } ?: "수면 읽기 권한과 데이터 공유 설정 확인 ›", style = MaterialTheme.typography.bodySmall)
            if (state == ConnectionVisualState.ERROR) Text("저장된 기록은 유지돼요. 연결 화면에서 다시 확인할 수 있어요.", style = MaterialTheme.typography.bodySmall)
        }
    }
}
