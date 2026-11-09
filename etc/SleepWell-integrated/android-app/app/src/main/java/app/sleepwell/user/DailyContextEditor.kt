package app.sleepwell.user

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.time.Instant
import java.time.ZoneId

@Composable
fun DailyContextEditor(value: DailyContext?, enabled: Boolean = true, onSave: (DailyContext) -> Unit) {
    val zone = ZoneId.of("Asia/Seoul")
    val today = Instant.now().atZone(zone).toLocalDate().toString()
    val current = value?.takeIf { it.nightId == today }
    fun clock(v: String?): String = v?.let { runCatching {
        Instant.parse(it).atZone(zone).toLocalTime().toString().take(5)
    }.getOrDefault("") } ?: ""
    var kind by remember(today, current) { mutableStateOf(current?.kind ?: "first") }
    var first by remember(today, current) { mutableStateOf(clock(current?.firstStart)) }
    var prep by remember(today, current) { mutableStateOf((current?.preparationMinutes ?: 30).toString()) }
    var napStart by remember(today, current) { mutableStateOf(clock(current?.napStart)) }
    var napEnd by remember(today, current) { mutableStateOf(clock(current?.napEnd)) }
    var caffeine by remember(today, current) { mutableStateOf(clock(current?.caffeineAt)) }
    var mg by remember(today, current) { mutableStateOf(current?.caffeineMg?.toString() ?: "") }
    var feedback by remember(today) { mutableStateOf<String?>(null) }
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("내일 일정과 오늘의 생활", style = MaterialTheme.typography.titleMedium)
            Text("$today 기준 · 가장 이른 일정, 낮잠 1회, 카페인 1회를 기록합니다. 시각은 07:30 형식입니다.")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("work" to "근무", "class" to "수업", "first" to "첫 일정").forEach { (id, label) ->
                    FilterChip(selected = kind == id, onClick = { kind = id }, label = { Text(label) })
                }
            }
            OutlinedTextField(first, { first = it }, label = { Text("내일 첫 일정 시각 · 없으면 비움") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(prep, { prep = it }, label = { Text("기상 후 준비·이동 시간(분)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(napStart, { napStart = it }, label = { Text("오늘 낮잠 시작 · 선택") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(napEnd, { napEnd = it }, label = { Text("오늘 낮잠 종료 · 선택") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(caffeine, { caffeine = it }, label = { Text("오늘 카페인 섭취 시각 · 선택") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(mg, { mg = it }, label = { Text("카페인 양(mg) · 선택") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Text("첫 일정은 권장 기상시간에 반영합니다. 낮잠·카페인은 기록과 안내에 사용하며 학습된 효과 계수는 아직 없습니다.")
            Button(onClick = {
                try {
                    onSave(buildDailyContext(today, kind, first, prep, napStart, napEnd, caffeine, mg))
                    feedback = null
                } catch (error: Exception) {
                    feedback = error.message ?: "생활 정보를 확인해주세요."
                }
            }, enabled = enabled) { Text("생활 정보 저장") }
            feedback?.let { Text(it) }
        }
    }
}
