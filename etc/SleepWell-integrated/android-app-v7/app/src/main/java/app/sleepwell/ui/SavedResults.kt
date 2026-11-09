package app.sleepwell.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.ResultStore
import app.sleepwell.analysis.SavedResult
import app.sleepwell.analysis.SleepDataStore
import app.sleepwell.analysis.SleepHistoryUi
import app.sleepwell.analysis.SurveyDay
import app.sleepwell.analysis.SurveyStore
import java.io.File
import java.text.SimpleDateFormat
import java.time.ZoneId
import java.util.Date
import java.util.Locale

/*
 * 저장 기록 화면 (소빈 · 2026-10-24) — 작업 지시서 Ⅲ-4 "결과 저장 및 조회"
 *  - 「다음 수면 일정」에서 저장한 결과를 최신 기기 시간대로 다시 표시한다.
 *    시간 원본이 없는 기존 행은 저장 당시 문구와 시간대 출처 안내를 그대로 보여준다.
 *  - 저장 파일: 앱 전용 폴더 saved_results.csv (ResultStore). 폰 안에만 있고 서버로 보내지 않는다.
 */

/** 분석 화면과 저장 기록 화면이 같은 파일을 쓰도록 한 곳에서 정한다 */
fun savedResultsFile(context: android.content.Context) = File(context.filesDir, "saved_results.csv")

/** (2026-10-24) 실제 컨디션 입력에서 쌓인 설문 (웹 설문 CSV 내보내기와 같은 형식) */
fun surveyLogFile(context: android.content.Context) = File(context.filesDir, "survey_log.csv")

@Composable
fun SavedResultsScreen() {
    val context = LocalContext.current
    val displayZone = ZoneId.of(UiZone.current.zoneId)
    val file = remember { savedResultsFile(context) }
    var items by remember { mutableStateOf(ResultStore.load(file)) }
    val surveyFile = remember { surveyLogFile(context) }
    var surveys by remember { mutableStateOf(SurveyStore.load(surveyFile)) }
    var history by remember { mutableStateOf(SleepHistoryUi.load(context)) }
    var historyLimit by remember { mutableStateOf(14) }
    var showSavedResults by remember { mutableStateOf(false) }
    var showDataManagement by remember { mutableStateOf(false) }
    var exportMessage by remember { mutableStateOf<String?>(null) }
    // (v6) 보관한 수면 기록 지우기
    val sleepDir = remember { File(context.filesDir, "sleep_data") }
    var hasSleepData by remember { mutableStateOf(SleepDataStore.hasAny(sleepDir)) }
    var confirmClear by remember { mutableStateOf(false) }
    var clearMessage by remember { mutableStateOf<String?>(null) }
    // 설문 기록 내보내기: 사용자가 고른 곳(다운로드 폴더 등)에 CSV 로 쓴다 → 서윤 언니 학습 코드에 그대로 넣을 수 있는 형식
    val exporter = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("text/csv")) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        exportMessage = try {
            val mine = surveys.filter { it.participant == "me" }
            context.contentResolver.openOutputStream(uri)?.use { it.write(SurveyStore.toCsv(mine).toByteArray(Charsets.UTF_8)) }
            "내 설문 ${mine.size}개를 내보냈어요."
        } catch (e: Exception) { "내보내지 못했어요: ${e.message}" }
    }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("수면 기록", style = MaterialTheme.typography.headlineSmall)
        Text(
            "삼성 헬스·헬스 커넥트와 직접 기록, 보완 설문을 기상일별로 합쳐 보여줘요. 시각은 현재 기기 시간대예요.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(Modifier.height(14.dp))
        RecentSleepOverview(history, displayZone)
        Spacer(Modifier.height(18.dp))
        Text("날짜별 주 수면", style = MaterialTheme.typography.titleMedium)
        if (history.isEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text("표시할 주 수면 기록이 없어요. 불러온 기록 중 3시간 이상인 주 수면을 날짜별로 표시하며, 필요한 항목은 앱 설문으로 보완합니다.")
        } else {
            history.take(historyLimit).forEach { night ->
                Spacer(Modifier.height(10.dp))
                SleepHistoryNightCard(night, displayZone)
            }
            if (historyLimit < history.size) {
                TextButton(onClick = { historyLimit += 30 }, modifier = Modifier.fillMaxWidth()) {
                    Text("이전 기록 더 보기 (${history.size - historyLimit}개)")
                }
            }
        }

        Spacer(Modifier.height(28.dp))
        DisclosureButton("저장한 과거 분석 결과", items.count { !it.isSyntheticResult() }, showSavedResults) {
            showSavedResults = !showSavedResults
        }
        if (showSavedResults) {
            val visibleItems = items.filterNot { it.isSyntheticResult() }
            if (visibleItems.isEmpty()) Text("아직 저장한 분석 결과가 없어요.", style = MaterialTheme.typography.bodyMedium)
            visibleItems.forEach { result ->
                Spacer(Modifier.height(10.dp))
                SavedCard(result, onDelete = { items = ResultStore.delete(file, result.savedAtMs) })
            }
        }

        Spacer(Modifier.height(12.dp))
        DisclosureButton("데이터 관리·내보내기", surveys.count { it.participant == "me" }, showDataManagement) {
            showDataManagement = !showDataManagement
        }
        if (showDataManagement) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("합성(Test) 자료는 일반 기록·내보내기에서 숨겨지며, 원본은 삭제하지 않아요.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedButton(
                    onClick = { exportMessage = null; exporter.launch("sleepwell_my_survey_log.csv") },
                    enabled = surveys.any { it.participant == "me" }, modifier = Modifier.fillMaxWidth()
                ) { Text("내 설문 CSV 내보내기") }
                exportMessage?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                surveys.filter { it.participant == "me" }.forEach { survey ->
                    SurveyRow(survey, onDelete = {
                        surveys = SurveyStore.delete(surveyFile, survey.participant, survey.date, survey.sleepWakeDate)
                        history = SleepHistoryUi.load(context)
                    })
                }
                Text(if (hasSleepData) "삼성 헬스·헬스 커넥트 원본 보관 중" else "보관한 수면 원본이 없어요",
                    style = MaterialTheme.typography.labelMedium)
                OutlinedButton(onClick = { confirmClear = true }, enabled = hasSleepData, modifier = Modifier.fillMaxWidth()) {
                    Text("보관한 수면 원본 지우기")
                }
                clearMessage?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
        }
        if (confirmClear) {
            AlertDialog(
                onDismissRequest = { confirmClear = false },
                title = { Text("보관한 수면 기록을 지울까요?") },
                text = { Text("이 폰에 보관한 삼성 헬스 수면 기록이 지워져요. 되돌릴 수 없어요. (삼성 헬스 앱의 기록은 그대로예요)") },
                confirmButton = {
                    TextButton(onClick = {
                        confirmClear = false
                        clearMessage = try {
                            SleepDataStore.clear(sleepDir); hasSleepData = false
                            history = SleepHistoryUi.load(context)
                            "지웠어요. 직접 기록·설문·저장 결과는 그대로예요."
                        }
                            catch (e: Exception) { "지우지 못했어요: ${e.message}" }
                    }) { Text("지우기") }
                },
                dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("취소") } }
            )
        }
        Spacer(Modifier.height(40.dp))
    }
}

@Composable
private fun DisclosureButton(label: String, count: Int, expanded: Boolean, onClick: () -> Unit) {
    TextButton(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
        Text(if (expanded) "▲  $label ($count)" else "▼  $label ($count)")
    }
}

private fun SavedResult.isSyntheticResult(): Boolean {
    val normalized = source.lowercase(Locale.ROOT)
    return normalized.contains("test") || normalized.contains("demo") || normalized.contains("합성")
}

@Composable
private fun SavedCard(r: SavedResult, onDelete: () -> Unit) {
    val displayZone = UiZone.current
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    val savedText = SimpleDateFormat("M/d(E) HH:mm", Locale.KOREA).apply { timeZone = displayZone.tz }.format(Date(r.savedAtMs))
    val rendered = ResultStore.render(r, displayZone.tz)
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(rendered.basisText.replace("기준 ", "") + " 기준 일정", style = MaterialTheme.typography.titleSmall)
                    Text("$savedText 저장 · 데이터: ${r.source}", style = MaterialTheme.typography.labelSmall, color = muted)
                    Text(rendered.provenanceText, style = MaterialTheme.typography.labelSmall, color = muted)
                }
                TextButton(onClick = onDelete) { Text("삭제") }
            }
            Spacer(Modifier.height(6.dp))
            Line("권장", rendered.recommendationText, MaterialTheme.colorScheme.primary)
            Line("모델 예상", rendered.modelText, muted)
            Line("평소 패턴", rendered.habitText + " (최근 7일 중간값)", muted)
            Line("상태", "수면 압력 ${r.sValue} · 일주기 리듬 ${r.cValue} · 잠들기 쉬운 정도 ${r.pValue}", muted)
            if (r.surveyText.isNotBlank()) {
                val surveyZone = r.sourceZoneId ?: "시간대 미기록"
                Line("설문", "${r.surveyText} (저장 당시 $surveyZone 표시)", muted)
            }
            Text(r.statusText, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.tertiary)
        }
    }
}

@Composable
private fun Line(label: String, value: String, color: Color) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = color, modifier = Modifier.weight(0.28f))
        Text(value, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(0.72f))
    }
}

@Composable
private fun SurveyRow(d: SurveyDay, onDelete: () -> Unit) {
    val displayZone = ZoneId.of(UiZone.current.zoneId)
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
    ) {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                val shownDate = d.sleepWakeDate ?: d.date
                Text(shownDate.substring(5).replace('-', '/') + if (d.sleepWakeDate != null) "  수면 보완" else "",
                    style = MaterialTheme.typography.labelLarge)
                Text(d.summaryIn(displayZone), style = MaterialTheme.typography.bodySmall)
            }
            TextButton(onClick = onDelete) { Text("삭제") }
        }
    }
}
