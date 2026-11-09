package app.sleepwell.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import app.sleepwell.analysis.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import java.io.File
import java.text.SimpleDateFormat
import java.time.Instant
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZoneId
import java.util.Date
import java.util.Locale

fun savedResultsFile(context: android.content.Context) = File(context.filesDir, "saved_results.csv")
fun surveyLogFile(context: android.content.Context) = File(context.filesDir, "survey_log.csv")

@Composable
fun SavedResultsScreen() {
    val context = LocalContext.current
    val zone = ZoneId.of(UiZone.current.zoneId)
    val resultFile = remember { savedResultsFile(context) }
    val surveyFile = remember { surveyLogFile(context) }
    val sleepDir = remember { File(context.filesDir, "sleep_data") }
    var results by remember { mutableStateOf(ResultStore.load(resultFile)) }
    var surveys by remember { mutableStateOf(SurveyStore.load(surveyFile)) }
    var history by remember { mutableStateOf(SleepHistoryUi.load(context)) }
    var nowMs by remember { mutableLongStateOf(System.currentTimeMillis()) }
    var hasSleepData by remember { mutableStateOf(SleepDataStore.hasAny(sleepDir)) }
    var selectedDateText by rememberSaveable { mutableStateOf<String?>(null) }
    var openState by rememberSaveable { mutableStateOf<String?>(null) }
    var selectedRecordKey by rememberSaveable { mutableStateOf<String?>(null) }
    var monthText by rememberSaveable(zone.id) { mutableStateOf(YearMonth.from(Instant.ofEpochMilli(nowMs).atZone(zone)).toString()) }
    var showData by remember { mutableStateOf(false) }
    var showDemoResults by rememberSaveable { mutableStateOf(false) }
    var exportMessage by remember { mutableStateOf<String?>(null) }
    var confirmClear by remember { mutableStateOf(false) }
    var clearMessage by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(context, zone) {
        val owner = context as? LifecycleOwner ?: return@LaunchedEffect
        owner.lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            var stamp: List<Triple<String, Long, Long>>? = null
            var day: LocalDate? = null
            while (isActive) {
                nowMs = System.currentTimeMillis()
                val localDay = Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate()
                val nextStamp = withContext(Dispatchers.IO) {
                    listOf(resultFile, surveyFile, File(context.filesDir, DiaryStore.FILE_NAME))
                        .plus(sleepDir.listFiles()?.sortedBy { it.name }.orEmpty())
                        .map { Triple(it.absolutePath, it.lastModified(), it.length()) }
                }
                if (nextStamp != stamp || localDay != day) {
                    val loaded = withContext(Dispatchers.IO) {
                        Triple(ResultStore.load(resultFile), SurveyStore.load(surveyFile), SleepHistoryUi.load(context, nowMs))
                    }
                    results = loaded.first; surveys = loaded.second; history = loaded.third
                    hasSleepData = SleepDataStore.hasAny(sleepDir)
                    stamp = nextStamp; day = localDay
                }
                delay(15_000)
            }
        }
    }

    val exporter = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("text/csv")) { uri ->
        if (uri != null) exportMessage = try {
            val mine = surveys.filter { it.participant == "me" }
            val out = context.contentResolver.openOutputStream(uri) ?: error("선택한 파일을 쓸 수 없어요.")
            out.use { it.write(SurveyStore.toCsv(mine).toByteArray(Charsets.UTF_8)) }
            "내 설문 ${mine.size}개를 내보냈어요."
        } catch (e: Exception) { "내보내지 못했어요: ${e.message}" }
    }
    val today = Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate()
    val month = runCatching { YearMonth.parse(monthText) }.getOrDefault(YearMonth.from(today))
    val hasDemoResults = remember(results) { results.any { it.source.isSyntheticSource() } }
    val days = remember(history, results, surveys, zone, showDemoResults) {
        HistoryCalendarProjection.days(history, results, zone, surveys, includeSyntheticResults = showDemoResults)
    }
    val selectedDate = selectedDateText?.let { text -> runCatching { LocalDate.parse(text) }.getOrNull() }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        Text("수면 기록", style = MaterialTheme.typography.headlineSmall)
        Text("현재 기기 시간대 · ${zone.id} · 기상일 기준", style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(14.dp))
        HistoryCalendar(month, today, selectedDate, days,
            onPrevious = { monthText = month.minusMonths(1).toString() },
            onNext = { monthText = month.plusMonths(1).toString() },
            onToday = { monthText = YearMonth.from(today).toString(); selectedDateText = today.toString() },
            onSelect = { selectedDateText = it.toString(); openState = "day:$it" })
        Spacer(Modifier.height(10.dp)); CalendarLegend()
        if (hasDemoResults) Row(Modifier.fillMaxWidth().heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("시연 재분석 결과 보기", style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
            Switch(checked = showDemoResults, onCheckedChange = { showDemoResults = it })
        }
        Spacer(Modifier.height(12.dp))
        if (selectedDate == null) {
            Text("날짜를 누르면 수면 기록과 설문, 저장한 분석 결과를 함께 볼 수 있어요.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            val selected = days[selectedDate] ?: HistoryCalendarDay(selectedDate, emptyList(), emptyList(), emptyList())
            SelectedDaySummary(selected) { openState = "day:$selectedDate" }
        }
        if (days.isEmpty()) EmptyHistoryState()

        Spacer(Modifier.height(24.dp))
        FoldRow("데이터 관리·내보내기", showData,
            "설문 ${surveys.count { it.participant == "me" }}개 · 수면 원본 ${if (hasSleepData) "보관 중" else "없음"}") { showData = !showData }
        if (showData) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("합성 자료는 일반 기록과 내보내기에서 제외하고 실제 사용자 기록만 표시해요.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedButton(onClick = { exportMessage = null; exporter.launch("sleepwell_my_survey_log.csv") },
                    enabled = surveys.any { it.participant == "me" }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Text("내 설문 CSV 내보내기")
                }
                exportMessage?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                surveys.filter { it.participant == "me" }.forEach { survey ->
                    SurveyRow(survey) {
                        surveys = SurveyStore.delete(surveyFile, survey.participant, survey.date, survey.sleepWakeDate)
                        history = SleepHistoryUi.load(context)
                    }
                }
                Text(if (hasSleepData) "삼성 헬스·헬스 커넥트 원본 보관 중" else "보관한 수면 원본이 없어요")
                OutlinedButton(onClick = { confirmClear = true }, enabled = hasSleepData,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("보관한 수면 원본 지우기") }
                clearMessage?.let { Text(it, style = MaterialTheme.typography.labelSmall) }
            }
        }
        Spacer(Modifier.height(40.dp))
    }

    selectedDate?.let { date ->
        val selected = days[date] ?: HistoryCalendarDay(date, emptyList(), emptyList(), emptyList())
        if (openState == "day:$date") DayDetailSheet(selected, zone, onDismiss = { openState = null },
            onOpenSleep = { selectedRecordKey = it.recordKey(); openState = null },
            onOpenSaved = { openState = "saved:$date" })
        if (openState == "saved:$date") SavedResultDialog(selected, zone, onClose = { openState = null }) { result ->
            results = ResultStore.delete(resultFile, result.savedAtMs)
            runCatching { SavedSnapshot.delete(context.filesDir, result.savedAtMs) }
            if (results.none { HistoryCalendarProjection.resultDate(it, zone) == date }) openState = null
        }
    }
    history.firstOrNull { it.recordKey() == selectedRecordKey }?.let { night ->
        SleepHistoryDetailSheet(night, zone) { selectedRecordKey = null }
    }
    if (confirmClear) AlertDialog(onDismissRequest = { confirmClear = false }, title = { Text("보관한 수면 기록을 지울까요?") },
        text = { Text("이 폰에 보관한 삼성 헬스 수면 기록이 지워져요. 되돌릴 수 없어요. 삼성 헬스 앱의 기록은 그대로예요.") },
        confirmButton = { TextButton(onClick = {
            confirmClear = false
            clearMessage = try {
                SleepDataStore.clear(sleepDir)
                CalculationInputStore.clear(context.filesDir, File(context.cacheDir, "user_sleep_stage.csv"))
                hasSleepData = false
                history = SleepHistoryUi.load(context)
                "지웠어요. 직접 기록·설문·저장 결과는 그대로예요."
            }
            catch (e: Exception) { "지우지 못했어요: ${e.message}" }
        }) { Text("지우기") } }, dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("취소") } })
}

@Composable
internal fun HistoryCalendar(month: YearMonth, today: LocalDate, selected: LocalDate?, days: Map<LocalDate, HistoryCalendarDay>,
    onPrevious: () -> Unit, onNext: () -> Unit, onToday: () -> Unit, onSelect: (LocalDate) -> Unit) {
    Card(Modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
        Column(Modifier.padding(10.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = onPrevious, modifier = Modifier.size(48.dp).semantics { contentDescription = "이전 달" }) { Text("‹") }
                Text("${month.year}년 ${month.monthValue}월", style = MaterialTheme.typography.titleMedium,
                    textAlign = TextAlign.Center, modifier = Modifier.weight(1f))
                TextButton(onClick = onNext, modifier = Modifier.size(48.dp).semantics { contentDescription = "다음 달" }) { Text("›") }
            }
            Row(Modifier.fillMaxWidth()) { listOf("일", "월", "화", "수", "목", "금", "토").forEach {
                Text(it, style = MaterialTheme.typography.labelSmall, textAlign = TextAlign.Center,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f)) } }
            HistoryCalendarProjection.monthCells(month).chunked(7).forEach { week ->
                Row(Modifier.fillMaxWidth()) { week.forEach { date ->
                    Box(Modifier.weight(1f).heightIn(min = 56.dp), contentAlignment = Alignment.Center) {
                        if (date != null) CalendarDayCell(date, today, selected, days[date], onSelect)
                    }
                } }
            }
            if (YearMonth.from(today) != month || selected != today)
                TextButton(onClick = onToday, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("오늘로 이동") }
        }
    }
}

@Composable private fun CalendarDayCell(date: LocalDate, today: LocalDate, selected: LocalDate?, entry: HistoryCalendarDay?, onSelect: (LocalDate) -> Unit) {
    val chosen = date == selected
    Column(Modifier.fillMaxWidth().heightIn(min = 56.dp).clickable(role = Role.Button) { onSelect(date) }.semantics {
        role = Role.Button; contentDescription = "${date.monthValue}월 ${date.dayOfMonth}일" +
            listOfNotNull("수면 기록".takeIf { entry?.hasSleep == true }, "설문".takeIf { entry?.hasSurvey == true },
                "저장 결과".takeIf { entry?.hasSavedResult == true }).let { found ->
                if (found.isEmpty()) ", 기록 없음" else ", ${found.joinToString("과")} 있음"
            }
    }, horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        Surface(shape = CircleShape, color = if (chosen) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface,
            border = if (date == today && !chosen) BorderStroke(1.5.dp, MaterialTheme.colorScheme.primary) else null,
            modifier = Modifier.size(36.dp)) { Box(contentAlignment = Alignment.Center) {
            Text(date.dayOfMonth.toString(), color = if (chosen) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface)
        } }
        Row(Modifier.height(10.dp), horizontalArrangement = Arrangement.spacedBy(4.dp), verticalAlignment = Alignment.CenterVertically) {
            if (entry?.hasSleep == true) Box(Modifier.size(6.dp).background(MaterialTheme.colorScheme.secondary, CircleShape))
            if (entry?.hasSurvey == true) Box(Modifier.size(6.dp).background(MaterialTheme.colorScheme.primary, CircleShape))
            if (entry?.hasSavedResult == true) Box(Modifier.size(6.dp).background(MaterialTheme.colorScheme.tertiary, CircleShape))
        }
    }
}

@Composable private fun CalendarLegend() = Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(18.dp)) {
    LegendDot(MaterialTheme.colorScheme.secondary, "수면 기록"); LegendDot(MaterialTheme.colorScheme.primary, "설문")
    LegendDot(MaterialTheme.colorScheme.tertiary, "저장한 분석 결과")
}
@Composable private fun LegendDot(color: androidx.compose.ui.graphics.Color, label: String) = Row(verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(6.dp)) { Box(Modifier.size(7.dp).background(color, CircleShape)); Text(label, style = MaterialTheme.typography.labelSmall) }

@Composable internal fun SelectedDaySummary(day: HistoryCalendarDay, onOpen: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(role = Role.Button, onClick = onOpen), colors = CardDefaults.cardColors(MaterialTheme.colorScheme.surfaceContainerLow),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
        Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("${day.date.monthValue}/${day.date.dayOfMonth}(${dayOfWeekKo(day.date)})", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text("수면 ${day.sleepRecords.size}개 · 설문 ${day.surveys.size}개 · 저장 결과 ${day.savedResults.size}개")
                if (!day.hasSleep && !day.hasSurvey && !day.hasSavedResult) Text("기록 없음 · 0시간으로 계산하지 않아요.", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }; Text("›")
        }
    }
}

@Composable private fun EmptyHistoryState() { Spacer(Modifier.height(14.dp)); Card(colors = CardDefaults.cardColors(MaterialTheme.colorScheme.surfaceContainerLow)) {
    Column(Modifier.padding(16.dp)) { Text("아직 불러온 기록이 없어요", style = MaterialTheme.typography.titleMedium)
        Text("삼성 헬스 기록이 동기화되거나 결과를 저장하면 이 달력에 표시돼요. 기록이 없는 날짜는 0시간으로 계산하지 않아요.", style = MaterialTheme.typography.bodySmall) }
} }

@OptIn(ExperimentalMaterial3Api::class)
@Composable internal fun DayDetailSheet(day: HistoryCalendarDay, zone: ZoneId, onDismiss: () -> Unit,
    onOpenSleep: (SleepHistoryNight) -> Unit, onOpenSaved: () -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(.88f).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("${day.date.monthValue}/${day.date.dayOfMonth}(${dayOfWeekKo(day.date)}) 상세", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text("현재 기기 시간대 · ${zone.id}", style = MaterialTheme.typography.bodySmall)
            if (day.sleepRecords.isEmpty()) Text("수면 기록이 없어요.")
            day.sleepRecords.forEach { night -> SleepSummaryRow(night, zone) { onOpenSleep(night) } }
            HorizontalDivider(); Text("그날 설문", style = MaterialTheme.typography.titleMedium)
            if (day.surveys.isEmpty()) Text("저장한 설문이 없어요.") else day.surveys.forEach { survey ->
                SurveyHistorySummary(survey, zone)
            }
            HorizontalDivider(); Text("저장한 분석 결과", style = MaterialTheme.typography.titleMedium)
            if (day.savedResults.isEmpty()) Text("저장한 결과가 없어요.") else {
                day.savedResults.forEach { SavedSummaryRow(it) }
                OutlinedButton(onClick = onOpenSaved, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("저장 결과 ${day.savedResults.size}개 자세히 보기") }
            }
        }
    }
}

@Composable private fun SurveyHistorySummary(survey: SurveyDay, zone: ZoneId) {
    val label = if (survey.sleepWakeDate != null) "기상 후 입력" else "수면 준비 · 취침 전 입력"
    Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(MaterialTheme.colorScheme.surfaceContainerLow)) {
        Column(Modifier.padding(14.dp)) {
            Text(label, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary)
            Text(survey.summaryIn(zone), style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable private fun SleepSummaryRow(night: SleepHistoryNight, zone: ZoneId, onClick: () -> Unit) {
    val fmt = remember(zone) { java.time.format.DateTimeFormatter.ofPattern("HH:mm").withZone(zone) }
    Card(Modifier.fillMaxWidth().clickable(role = Role.Button, onClick = onClick), colors = CardDefaults.cardColors(MaterialTheme.colorScheme.surfaceContainerLow)) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) { Column(Modifier.weight(1f)) {
            Text("${fmt.format(Instant.ofEpochMilli(night.bedtimeMs))} 취침 → ${fmt.format(Instant.ofEpochMilli(night.wakeMs))} 기상", style = MaterialTheme.typography.titleSmall)
            Text("기록 구간 ${historyDurationText(night.intervalMinutes)} · ${night.sourceLabel}", style = MaterialTheme.typography.bodySmall)
        }; Text("›") }
    }
}

@Composable private fun SavedSummaryRow(result: SavedResult) { val shown = ResultStore.render(result, UiZone.current.tz)
    Card(colors = CardDefaults.cardColors(MaterialTheme.colorScheme.surface), border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
        Column(Modifier.padding(12.dp)) { Text(shown.recommendationText, color = MaterialTheme.colorScheme.primary)
            if (result.profileText.isNotBlank()) Text("내 정보 · ${result.profileText}", style = MaterialTheme.typography.bodySmall)
            if (result.surveyText.isNotBlank()) Text("그날 입력 · ${result.surveyText}", style = MaterialTheme.typography.bodySmall)
            Text(shown.provenanceText, style = MaterialTheme.typography.labelSmall) }
    }
}

@Composable private fun SavedResultDialog(day: HistoryCalendarDay, zone: ZoneId, onClose: () -> Unit, onDelete: (SavedResult) -> Unit) {
    val context = LocalContext.current
    val newestId = day.savedResults.maxOfOrNull { it.savedAtMs }
    var selectedSavedId by remember(day.date) { mutableStateOf(newestId) }
    LaunchedEffect(day.savedResults.map { it.savedAtMs }) {
        if (day.savedResults.none { it.savedAtMs == selectedSavedId }) selectedSavedId = newestId
    }
    val selectedResult = day.savedResults.firstOrNull { it.savedAtMs == selectedSavedId }
    val snapshotId = selectedSavedId?.takeIf { SavedSnapshot.exists(context.filesDir, it) }
    var replay by remember(snapshotId) { mutableStateOf<SavedSnapshot.Replay?>(null) }
    var loading by remember(snapshotId) { mutableStateOf(snapshotId != null) }
    LaunchedEffect(snapshotId, UiZone.current.zoneId) {
        replay = snapshotId?.let { id -> withContext(Dispatchers.IO) { SavedSnapshot.load(context.filesDir, id)?.let(SavedSnapshot::replay) } }
        loading = false
    }
    Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) { Column {
            TextButton(onClick = onClose, modifier = Modifier.heightIn(min = 48.dp)) { Text("← 기록") }
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp)) {
                Text("${day.date.monthValue}/${day.date.dayOfMonth}(${dayOfWeekKo(day.date)}) 저장 결과", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                Text("현재 기기 시간대 · ${zone.id}", style = MaterialTheme.typography.bodySmall); Spacer(Modifier.height(12.dp))
                selectedResult?.let {
                    Text(ResultStore.originLabel(it.predictionOrigin), style = MaterialTheme.typography.titleSmall,
                        color = MaterialTheme.colorScheme.primary)
                    Text("저장된 결과를 그대로 표시하며 다시 계산하지 않아요.", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(12.dp))
                }
                when { loading -> Row { CircularProgressIndicator(Modifier.size(22.dp)); Text("  저장한 분석을 불러오는 중…") }
                    replay?.display != null -> { ModelRhythmCard(replay!!.display, replay!!.display!!.referenceMs); AnalysisContent(
                        d = replay!!.display!!, sourceName = selectedResult?.source.orEmpty(), zone = UiZone.current,
                        onAskTargetWake = {}, onSave = null, showSchedule = false) }
                    selectedResult != null && snapshotId == null -> Text("이 결과에는 그래프 스냅샷이 없어 저장된 요약을 표시해요.")
                    else -> Text(replay?.error?.title ?: "저장한 분석을 불러오지 못했어요.", color = MaterialTheme.colorScheme.error) }
                selectedResult?.let { result ->
                    val saved = SimpleDateFormat("M/d(E) HH:mm", Locale.KOREA).apply { timeZone = UiZone.current.tz }.format(Date(result.savedAtMs))
                    Text(if (replay?.display != null) "위 그래프 · $saved 저장 결과" else "$saved 저장 결과", style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(20.dp)); Text("저장한 일정", style = MaterialTheme.typography.titleMedium)
                day.savedResults.forEach { result ->
                    Spacer(Modifier.height(10.dp))
                    SavedEntry(result, selected = result.savedAtMs == selectedSavedId,
                        onView = { selectedSavedId = result.savedAtMs }, onDelete = onDelete)
                }
                Spacer(Modifier.height(32.dp))
            }
        } }
    }
}

@Composable private fun SavedEntry(result: SavedResult, selected: Boolean, onView: () -> Unit, onDelete: (SavedResult) -> Unit) { val shown = ResultStore.render(result, UiZone.current.tz)
    val saved = SimpleDateFormat("M/d(E) HH:mm", Locale.KOREA).apply { timeZone = UiZone.current.tz }.format(Date(result.savedAtMs))
    Card(Modifier.fillMaxWidth(), border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) { Column(Modifier.padding(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) { Text("$saved 저장", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f));
            TextButton(onClick = { onDelete(result) }, modifier = Modifier.heightIn(min = 48.dp)) { Text("삭제") } }
        Text(ResultStore.originLabel(result.predictionOrigin), style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.primary)
        if (result.profileText.isNotBlank()) Line("내 정보", result.profileText, MaterialTheme.colorScheme.secondary)
        if (result.surveyText.isNotBlank()) Line("그날 입력", result.surveyText, MaterialTheme.colorScheme.secondary)
        Line("예상", shown.modelText, MaterialTheme.colorScheme.onSurfaceVariant)
        if (result.isPast && shown.actualText.isNotBlank()) Line("실제", shown.actualText, MaterialTheme.colorScheme.tertiary)
        else Line("권장", shown.recommendationText, MaterialTheme.colorScheme.primary)
        Line("평소 패턴", shown.habitText, MaterialTheme.colorScheme.onSurfaceVariant); Text(shown.provenanceText, style = MaterialTheme.typography.labelSmall)
        OutlinedButton(onClick = onView, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp), enabled = !selected) {
            Text(if (selected) "위 그래프에 표시 중" else "이 결과 그래프 보기")
        }
    } }
}

@Composable private fun FoldRow(title: String, open: Boolean, sub: String, onToggle: () -> Unit) { Card(Modifier.fillMaxWidth().clickable(onClick = onToggle),
    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) { Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
    Column(Modifier.weight(1f)) { Text(title); Text(sub, style = MaterialTheme.typography.labelSmall) }; Text(if (open) "▾" else "›") } } }
@Composable private fun Line(label: String, value: String, color: androidx.compose.ui.graphics.Color) { Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
    Text(label, style = MaterialTheme.typography.labelMedium, color = color, modifier = Modifier.weight(.28f)); Text(value, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(.72f)) } }
@Composable private fun SurveyRow(day: SurveyDay, onDelete: () -> Unit) { val zone = ZoneId.of(UiZone.current.zoneId)
    Card(Modifier.fillMaxWidth(), border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) { Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) { val shown = day.sleepWakeDate ?: day.date; Text(shown.substring(5).replace('-', '/')); Text(day.summaryIn(zone), style = MaterialTheme.typography.bodySmall) }
        TextButton(onClick = onDelete, modifier = Modifier.heightIn(min = 48.dp)) { Text("삭제") } } } }

private fun SleepHistoryNight.recordKey() = "$day|$bedtimeMs|$wakeMs|$source"
private fun dayOfWeekKo(date: LocalDate) = "월화수목금토일"[date.dayOfWeek.value - 1].toString()
private fun historyDurationText(minutes: Int) = if (minutes >= 60) "${minutes / 60}시간 ${minutes % 60}분" else "${minutes}분"
