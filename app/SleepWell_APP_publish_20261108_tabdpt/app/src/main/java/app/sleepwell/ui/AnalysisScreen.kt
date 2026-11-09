package app.sleepwell.ui

import android.content.Context
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.FilterChip
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TextButton
import androidx.compose.material3.Text
import androidx.compose.material3.Surface
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.listSaver
import app.sleepwell.analysis.SleepHistoryUi
import app.sleepwell.analysis.SleepHistoryNight
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.border
import androidx.compose.ui.text.style.LineBreak
import androidx.compose.ui.unit.sp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import app.sleepwell.analysis.AnalysisDisplay
import app.sleepwell.analysis.AnalysisPresenter
import app.sleepwell.analysis.AnalysisRunner
import app.sleepwell.analysis.DiaryStore
import app.sleepwell.analysis.ImportedSleepStore
import app.sleepwell.analysis.ChartPoint
import app.sleepwell.analysis.FatigueScale
import app.sleepwell.analysis.DirectInputForm
import app.sleepwell.analysis.DirectInputParser
import app.sleepwell.analysis.Gauge
import app.sleepwell.analysis.Reading
import app.sleepwell.analysis.ReadingKind
import app.sleepwell.analysis.ResultStore
import app.sleepwell.analysis.SavedSnapshot
import app.sleepwell.analysis.CalculationInputStore
import app.sleepwell.analysis.SurveyFormRestore
import androidx.compose.runtime.DisposableEffect
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import app.sleepwell.analysis.ProfileStore
import app.sleepwell.analysis.NextNightModel
import app.sleepwell.analysis.SleepDataStore
import app.sleepwell.analysis.SurveyCsv
import app.sleepwell.analysis.SurveyDay
import app.sleepwell.analysis.SurveyObservationPolicy
import app.sleepwell.analysis.SurveyStore
import app.sleepwell.analysis.Notice
import app.sleepwell.analysis.HealthConnectSleep
import androidx.health.connect.client.PermissionController
import app.sleepwell.analysis.Seg
import app.sleepwell.analysis.SegStyle
import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.DisplayZone
import app.sleepwell.health.HealthResult
import app.sleepwell.health.SleepRecord
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import app.sleepwell.analysis.BackgroundSleepSync
import java.time.Instant
import java.util.Calendar

/*
 * 사용자 화면: 수면 분석 결과. (소빈 · 2026-09-25, 합의안 2절 ⑤~⑧)
 *
 * 흐름: CSV 선택 → (백그라운드) 채윤 파이프라인 → AnalysisResult → AnalysisPresenter 로 정리 → 화면
 *
 * - 채윤 파이프라인 호출은 AnalysisRunner 한 곳에서만 한다.
 * - 값은 임시 모델 결과라 확정값이 아니다 → 화면 맨 위에 "임시 모델" 표시.
 * - 권장 시각은 모델에 없어 "준비 중". futureS 는 채윤 코드가 임시 예측을 넣어서 "임시 예측"으로 표시.
 * - (2026-10-24) 권장 카드 → 「다음 수면 일정」 카드(ScheduleCard). 예상(모델·내 기록)과 권장을 나눠 보여준다.
 *   목표 기상이 없으면 "목표 기상 입력하기" → 실제 컨디션 입력 칸으로 스크롤.
 * - (2026-10) 날짜(밤) 선택: ◀ ▶ 와 달력으로 "○○ 밤"(그날 12:00 ~ 다음 날 12:00)을 골라 본다.
 *   처음에는 가장 최근 밤. "지금 상태"·예측은 가장 최근 밤에서만 보인다.
 *   가장 최근 밤은 업로드 때 계산한 결과(마지막 기록 기준 앞 7일)를 쓰고,
 *   지난 밤을 고르면 그 밤의 끝을 기준 시각으로 다시 계산한다(그 앞 7일). 고를 수 있는 밤 = 데이터 전체 범위. (2026-10)
 */

/** 계산 결과 + 그때 쓴 입력. 밤·시간대를 바꿀 때 같은 입력으로 화면만 다시 정리하기 위해 함께 둔다. */
private data class ModelInputs(
    val profile: NextNightModel.ModelProfile,
    val surveys: List<SurveyDay>,
    val diaries: List<app.sleepwell.user.SleepRecord>,
    val imports: List<app.sleepwell.user.SleepRecord>,
    val wearableEnabled: Boolean
) {
    companion object {
        fun capture() = ModelInputs(NextNightModel.profile, NextNightModel.surveyDays.toList(),
            NextNightModel.diaryRecords.toList(), NextNightModel.importedRecords.toList(),
            NextNightModel.wearablePersonalizationEnabled)
    }
}
private data class Computed(
    val result: AnalysisResult, val direct: DirectInput?, val condition: Int?, val survey: SurveyDay? = null,
    val input: SavedSnapshot.CapturedInput? = null, val modelInputs: ModelInputs = ModelInputs.capture(),
    val source: String = "내 수면 기록"
)
private data class AnalysisWork(
    val result: HealthResult<AnalysisResult>,
    val importedNights: List<app.sleepwell.user.SleepRecord>,
    val reanalysisCandidates: List<app.sleepwell.user.SleepRecord>,
    val diaryCount: Int,
    val historyNights: List<SleepHistoryNight>,
    val sourceName: String
)
/** The result actually on screen, together with the exact inputs and presentation references. */
private data class DisplayedCalculation(
    val computed: Computed, val dayMs: Long?, val basisMs: Long?, val latestRefMs: Long,
    val dataStartMs: Long?, val recommendationReferenceMs: Long?
)

private val COLOR_S = Color(0xFF3D6FB6)      // 수면 압력: 파랑
private val COLOR_C = Color(0xFFE8853A)      // 일주기 리듬: 주황
private val COLOR_P = Color(0xFF7B4FC9)      // 수면 성향: 보라
private val COLOR_SLEEP_BAND = Color(0x2E3D6FB6)       // (2026-10-24) 실제로 잔 시간: 연한 파란 바탕
private val COLOR_SLEEP_SWATCH = Color(0x663D6FB6)     // 설명 글 속 같은 색 네모
private val COLOR_PRED_BAR = Color(0xFF8A94A3)         // 예상: 회색 막대
private val COLOR_REC_BAR = Color(0xFF2E9D5B)          // 권장: 초록 막대
private val COLOR_MEANING = Color(0xFFFFF4CC)          // 해석 "무슨 뜻인가" 노란 바탕
private val COLOR_MANUAL_BAND = Color(0x297B4FC9)
private val COLOR_SOFT_BOX = Color(0xFFF3F4F7)       // 체크 설명·경향 문장 배경
private val COLOR_NOTE: Color
    @Composable get() = MaterialTheme.colorScheme.onSurfaceVariant

/** (2026-10-27) CSV 직접 선택 안내 — 삼성 헬스 내보내기에는 파일이 많아서 */
private const val CSV_FILE_GUIDE = "삼성 헬스 내보내기 파일 중 com.samsung.health.sleep_stage…csv (이름에 'stage'가 있는 파일)를 골라 주세요."

internal val inputDraftSaver = listSaver<DirectInputForm, String>(
    save = { listOf(it.bedText, it.wakeText, it.plannedBedText, it.targetWakeText,
        it.sleepLatencyText, it.awakeMinutesText, it.condition?.toString().orEmpty(), it.rested?.toString().orEmpty(),
        it.caffeineMgText, it.lastCaffeineText, it.napMinText, it.napEndText, it.exerciseMinText,
        it.exerciseIntensity.orEmpty(), it.sleepWakeDate.orEmpty(), it.fatigueScaleMax.toString()) },
    restore = { DirectInputForm(it[0], it[1], it[2], it[3], it[4], it[5], it[6].toIntOrNull(), it[7].toIntOrNull(),
        it[8], it[9], it[10], it[11], it[12], it[13].ifBlank { null }, it[14].ifBlank { null },
        if (it.size <= 15) FatigueScale.LEGACY_MAX else it[15].toIntOrNull() ?: 0) }
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AnalysisScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var nowMs by remember { mutableStateOf(System.currentTimeMillis()) }
    // Includes resume, date rollover and user/network clock corrections without reopening the file.
    LaunchedEffect(Unit) {
        while (true) {
            nowMs = System.currentTimeMillis()
            delay(15_000)
        }
    }

    var loading by remember { mutableStateOf(false) }
    var sourceName by remember { mutableStateOf<String?>(null) }
    var display by remember { mutableStateOf<AnalysisDisplay?>(null) }
    var error by remember { mutableStateOf<Notice?>(null) }          // (2026-10-24) 이유 코드별 안내 카드
    var computed by remember { mutableStateOf<Computed?>(null) }
    var displayedCalculation by remember { mutableStateOf<DisplayedCalculation?>(null) }
    var saving by remember { mutableStateOf(false) }
    var analysisJob by remember { mutableStateOf<Job?>(null) }
    var analysisGeneration by remember { mutableStateOf(0) }
    val analysisMutex = remember { Mutex() }
    val capturedInputs = remember { mutableListOf<SavedSnapshot.CapturedInput>() }
    DisposableEffect(Unit) {
        onDispose { capturedInputs.forEach { SavedSnapshot.releaseCapturedInput(context.filesDir, it) } }
    }
    var selectedNight by remember { mutableStateOf<Long?>(null) }   // null = 가장 최근 밤
    var pickMessage by remember { mutableStateOf<String?>(null) }
    var nightLoading by remember { mutableStateOf(false) }          // 지난 밤 다시 계산 중
    var nightError by remember { mutableStateOf<Notice?>(null) }    // 지난 날을 계산할 수 없을 때 이유
    val zone = UiZone.current                                        // 표시 시간대 (바꾸면 아래 LaunchedEffect 가 다시 정리)

    // ── 4단계 직접 입력 상태 ──
    var fileReady by remember { mutableStateOf(false) }          // 앱 전용 영구 계산 CSV 준비 여부
    var form by rememberSaveable(stateSaver = inputDraftSaver) { mutableStateOf(DirectInputForm()) }
    var formZoneId by rememberSaveable { mutableStateOf(zone.zoneId) }
    var formDirty by rememberSaveable { mutableStateOf(false) }
    var restoredSurvey by remember { mutableStateOf<SurveyFormRestore.Restored?>(null) }
    var restoreRevision by remember { mutableStateOf(0) }
    // 미완성 입력은 시각으로 해석할 수 없으므로 작성 시작 시점의 시간대를 유지한다.
    LaunchedEffect(zone, form) {
        if (form == DirectInputForm()) formZoneId = zone.zoneId
    }
    var formErrors by remember { mutableStateOf<List<String>>(emptyList()) }
    var appliedInput by remember { mutableStateOf<DirectInput?>(null) }
    var appliedCondition by remember { mutableStateOf<Int?>(null) }
    var lastRecordEnd by remember { mutableStateOf<Long?>(null) }   // 업로드 기록의 마지막 기상

    val uploaded = remember { CalculationInputStore.workingFile(context.filesDir) }
    var inputPrepared by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        try {
            fileReady = withContext(Dispatchers.IO) {
                CalculationInputStore.prepare(context.filesDir, File(context.cacheDir, "user_sleep_stage.csv")) != null
            }
        } catch (e: Exception) { error = Notice("보관한 계산 파일을 열지 못했어요", listOf(e.message.orEmpty())) }
        inputPrepared = true
    }
    val diaryFile = remember { File(context.filesDir, DiaryStore.FILE_NAME) }
    val dataDir = remember { File(context.filesDir, "sleep_data") }
    var importedNights by remember { mutableStateOf<List<app.sleepwell.user.SleepRecord>>(emptyList()) }
    var reanalysisCandidates by remember { mutableStateOf<List<app.sleepwell.user.SleepRecord>>(emptyList()) }
    var diaryCount by remember { mutableStateOf(0) }
    val scroll = rememberScrollState()
    // (2026-10-24) 취침 전 설문은 앱 안에서 직접 입력 (실제 컨디션 입력 칸). 설문 CSV 올리기는 뺐다 —
    //  CSV 내보내기 파일은 서윤 언니 개인 보정 학습용. 지금 모델 계산에는 오늘 값만 필요하고, 그중 목표 기상만 쓰인다.
    var appliedSurvey by remember { mutableStateOf<SurveyDay?>(null) }
    var displayedSurvey by remember { mutableStateOf<SurveyDay?>(null) }
    // (2026-10-24 팀장 답변) 입력한 설문은 폰 안 survey_log.csv 에 날짜별로 쌓는다 → 개인 보정 학습 데이터
    val surveyFile = remember { surveyLogFile(context) }
    var surveyNote by remember { mutableStateOf<String?>(null) }
    // (2026-10-24) 결과 저장 (폰 안 파일, 저장 기록 탭에서 조회)
    val storeFile = remember { savedResultsFile(context) }
    var saveMessage by remember { mutableStateOf<String?>(null) }
    var showConnection by rememberSaveable { mutableStateOf(false) }
    var showInput by rememberSaveable { mutableStateOf(false) }
    var showDetails by rememberSaveable { mutableStateOf(false) }
    var manualEntry by rememberSaveable { mutableStateOf(false) }
    var historyNights by remember { mutableStateOf<List<SleepHistoryNight>>(emptyList()) }
    var healthConnected by remember { mutableStateOf<Boolean?>(null) }
    var lastForegroundCheck by remember { mutableStateOf<Long?>(null) }

    // 영구 보관 CSV의 불변 사본과 직접 입력으로 계산한다.
    fun runAnalysis(direct: DirectInput?, condition: Int?, survey: SurveyDay? = null, preserveSelection: Boolean = false) {
        analysisJob?.cancel()
        val generation = ++analysisGeneration
        saveMessage = null
        loading = true
        error = null
        display = null
        displayedSurvey = null
        displayedCalculation = null
        computed = null
        pickMessage = null
        analysisJob = scope.launch {
            var frozenInput: SavedSnapshot.CapturedInput? = null
            var handedOff = false
            var frozenModelInputs = ModelInputs.capture()
            var analysisSource = sourceName ?: "내 수면 기록"
            try {
                val work = withContext(Dispatchers.IO) {
                    analysisMutex.withLock {
                        if (generation != analysisGeneration) return@withLock null
                        val who = "me"
                        val surveyDays = SurveyStore.load(surveyFile).filter { it.participant == who }
                        NextNightModel.surveyDays = surveyDays
                        NextNightModel.demoMode = false
                        var workImportedNights = emptyList<app.sleepwell.user.SleepRecord>()
                        var workReanalysisCandidates = emptyList<app.sleepwell.user.SleepRecord>()
                        var workDiaryCount = 0
                        var workHistoryNights = emptyList<SleepHistoryNight>()
                        var workSourceName = analysisSource
                        try {
                            val diaryRecords = DiaryStore.load(diaryFile)
                            NextNightModel.diaryRecords = diaryRecords
                            val analysisNow = System.currentTimeMillis()
                            NextNightModel.wearablePersonalizationEnabled = true
                            workImportedNights = ImportedSleepStore.loadModelNights(dataDir, analysisNow, emptyList())
                            workReanalysisCandidates = ImportedSleepStore.loadReanalysisCandidates(dataDir, analysisNow)
                            NextNightModel.importedRecords = ImportedSleepStore.loadModelNights(dataDir, analysisNow, surveyDays)
                            workDiaryCount = diaryRecords.size
                            workHistoryNights = SleepHistoryUi.load(context, analysisNow)
                            workSourceName = if (workHistoryNights.isEmpty()) "입력한 수면 습관 (반영 가능한 주 수면 기록 없음)"
                                else workHistoryNights.map { it.sourceLabel }.distinct().joinToString(" · ")
                            val features = survey?.featureValues() ?: emptyMap()
                            frozenInput = if (fileReady && uploaded.isFile) SavedSnapshot.captureInput(context.filesDir, uploaded) else null
                            frozenModelInputs = ModelInputs.capture()
                            val sensorResult = frozenInput?.let { AnalysisRunner.runFile(it.file, direct, features) }
                            val healthResult = if (sensorResult is HealthResult.Success) sensorResult
                            else if (frozenInput == null || workImportedNights.isNotEmpty())
                                HealthResult.Success(AnalysisRunner.runDiary(direct, features, analysisNow))
                            else sensorResult!!
                            AnalysisWork(healthResult, workImportedNights, workReanalysisCandidates,
                                workDiaryCount, workHistoryNights, workSourceName)
                        } catch (e: Exception) {
                            AnalysisWork(HealthResult.Failure(e), workImportedNights, workReanalysisCandidates,
                                workDiaryCount, workHistoryNights, workSourceName)
                        }
                    }
                } ?: return@launch
                ensureActive()
                if (generation != analysisGeneration) return@launch
                // Completed calculations replace the owner lease; in-flight display/save jobs hold their own.
                capturedInputs.forEach { SavedSnapshot.releaseCapturedInput(context.filesDir, it) }
                capturedInputs.clear()
                importedNights = work.importedNights
                reanalysisCandidates = work.reanalysisCandidates
                diaryCount = work.diaryCount
                historyNights = work.historyNights
                analysisSource = work.sourceName
                sourceName = work.sourceName
                when (val result = work.result) {
                    is HealthResult.Success -> {
                        frozenInput?.let { capturedInputs += it }
                        handedOff = true
                        if (direct == null) lastRecordEnd = listOfNotNull(result.data.sleepRecords.maxOfOrNull { it.endMs },
                            importedNights.maxOfOrNull { Instant.parse(it.wakeTime).toEpochMilli() }).maxOrNull()
                        if (!preserveSelection) selectedNight = null
                        computed = Computed(result.data, direct, condition, survey, frozenInput, frozenModelInputs, analysisSource)
                    }
                    else -> error = AnalysisPresenter.failureNotice(result)
                }
            } finally {
                if (!handedOff) SavedSnapshot.releaseCapturedInput(context.filesDir, frozenInput)
                if (generation == analysisGeneration) loading = false
            }
        }
    }

    // 업로드 직후 백그라운드에서 계산 (합의안 3-2). 새 파일이면 직접 입력은 비운다.
    fun analyze(name: String, copyTo: (File) -> Unit) {
        sourceName = name
        form = DirectInputForm(); formDirty = false; restoredSurvey = null; restoreRevision++; formZoneId = zone.zoneId; formErrors = emptyList(); appliedInput = null; appliedCondition = null; appliedSurvey = null; surveyNote = null; lastRecordEnd = null
        fileReady = try {
            val temporary = File(context.cacheDir, "selected_calculation_input.csv")
            try { copyTo(temporary); CalculationInputStore.replaceFrom(temporary, context.filesDir) }
            finally { temporary.delete() }
            true
        } catch (e: Exception) {
            error = Notice("파일을 읽지 못했어요", listOf(e.message ?: "알 수 없는 오류")); display = null; false
        }
        if (fileReady) runAnalysis(null, null)
    }

    // 입력이 바뀌면 다시 계산 (합의안 5-1 #3 "사용자 조건 변경 시 재계산")
    fun applyForm() {
        val parsed = DirectInputParser.parse(form, lastRecordEnd, System.currentTimeMillis(), java.time.ZoneId.of(formZoneId),
            linkedSleep = importedNights.firstOrNull { it.day == form.sleepWakeDate })
        formErrors = parsed.errors
        if (!parsed.ok) return
        parsed.diary?.let { record ->
            run {
                try { diaryCount = DiaryStore.save(diaryFile, record).size }
                catch (e: Exception) { formErrors = listOf("수면일지를 저장하지 못했어요: ${e.message}"); return }
            }
        }
        appliedInput = parsed.input
        appliedCondition = form.condition
        appliedSurvey = parsed.survey?.dailyContextPart()
        try {
            val prior = restoredSurvey
            val all = SurveyStore.saveRestoredEdit(surveyFile, parsed.survey?.copy(participant = "me"),
                prior?.context, prior?.sleepSupplement,
                emptyEditContextDate = NextNightModel.liveNightId(Instant.now().atZone(java.time.ZoneId.of(zone.zoneId)), NextNightModel.profile.usualWake).toString(),
                emptyEditSleepWakeDate = form.sleepWakeDate)
            surveyNote = "입력을 저장했어요 · 저장한 응답 ${all.count { it.participant == "me" }}개"
            formDirty = false
            restoreRevision++
        } catch (e: Exception) { formErrors = listOf("설문을 저장하지 못했어요: ${e.message}"); return }
        runAnalysis(appliedInput, appliedCondition, appliedSurvey)
    }

    // ── (v6 2026-10-31) 수면 기록 보관 + 합치기: 과거 = 내보내기 CSV, 앞으로 = 헬스 커넥트 (analysis/SleepDataStore.kt) ──
    // (v6) 내 정보(프로필) — 새 모델의 목표 수면시간 출발점 (analysis/ProfileStore.kt)
    val profileFile = remember { File(context.filesDir, ProfileStore.FILE_NAME) }
    val storedProfile = remember { ProfileStore.load(profileFile) }
    var profile by remember { mutableStateOf(NextNightModel.profile) }
    var profileSaved by remember { mutableStateOf(storedProfile != null) }
    var profileStoreWarning by remember {
        mutableStateOf(if (profileFile.isFile && storedProfile == null) "저장된 내 정보를 읽지 못해 기본값을 사용 중이에요. 다시 저장해 주세요." else null)
    }
    var storeMessage by remember { mutableStateOf<String?>(null) }
    /** 보관한 기록(내보내기 + 헬스 커넥트)을 합쳐서 분석. prepare = 보관 전에 할 일(새 파일 저장 등) */
    fun analyzeStored(preserveForm: Boolean = false, prepare: () -> Unit = {}) {
        loading = true
        scope.launch {
            val built = withContext(Dispatchers.IO) {
                try { prepare(); Result.success(SleepDataStore.buildMerged(dataDir)) } catch (e: Exception) { Result.failure<Pair<File, SleepDataStore.MergeInfo>?>(e) }
            }
            built.onSuccess { pair ->
                if (pair == null) { loading = false; return@onSuccess }
                val (merged, info) = pair
                storeMessage = "내보내기 기록 ${info.archiveSessions}번 + 헬스 커넥트 새 기록 ${info.healthConnectAdded}번을 합쳐 분석해요"
                val name = when {
                    info.archiveSessions > 0 && info.healthConnectAdded > 0 -> "삼성 헬스 (내보내기 + 연동)"
                    info.archiveSessions > 0 -> "삼성 헬스 내보내기"
                    else -> "헬스 커넥트"
                }
                if (preserveForm) {
                    try {
                        CalculationInputStore.replaceFrom(merged, context.filesDir)
                        sourceName = name
                        fileReady = true
                        runAnalysis(appliedInput, appliedCondition, appliedSurvey, preserveSelection = true)
                    } catch (e: Exception) { loading = false; error = Notice("파일을 읽지 못했어요", listOf(e.message.orEmpty())) }
                } else analyze(name) { dst -> merged.copyTo(dst, overwrite = true) }
            }.onFailure { e -> loading = false; error = Notice("파일을 읽지 못했어요", listOf(e.message ?: "알 수 없는 오류")) }
        }
    }
    /** 고른 CSV 를 과거 기록(내보내기)으로 보관하고 분석 */
    fun keepArchive(uri: Uri) = analyzeStored {
        val tmp = File(context.cacheDir, "picked_sleep_stage.csv")
        copyUri(context, uri, tmp)
        SleepDataStore.saveArchive(dataDir, tmp)
        tmp.delete()
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri != null) keepArchive(uri)
    }
    // (2026-10-27) 삼성 헬스 내보내기 **폴더**를 고르면 그 안의 sleep_stage CSV 를 앱이 찾아 읽는다
    var folderMessage by remember { mutableStateOf<String?>(null) }
    val folderPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { tree: Uri? ->
        if (tree == null) return@rememberLauncherForActivityResult
        folderMessage = null
        scope.launch {
            val found = withContext(Dispatchers.IO) {
                try { SamsungExportFolder.findSleepStageCsv(context, tree) } catch (e: Exception) { null }
            }
            if (found == null) {
                folderMessage = "이 폴더에서 수면 단계 파일(com.samsung.health.sleep_stage…csv)을 찾지 못했어요. " +
                    "삼성 헬스 내보내기 폴더(com.samsung… 로 시작하는 CSV 들이 있는 폴더)를 골라 주세요."
            } else {
                folderMessage = "찾은 파일: ${found.name}"
                keepArchive(found.uri)
            }
        }
    }

    // ── (2026-10-27) 삼성 헬스 → 헬스 커넥트에서 바로 불러오기 (CSV 없이) ──
    var hcBusy by remember { mutableStateOf(false) }
    var showCsvGuide by remember { mutableStateOf(false) }
    var hcMessage by remember { mutableStateOf<String?>(null) }
    var foregroundSyncFailed by remember { mutableStateOf(false) }
    var backgroundState by remember { mutableStateOf<BackgroundSleepSync.State?>(null) }
    val backgroundPermissionLauncher = rememberLauncherForActivityResult(PermissionController.createRequestPermissionResultContract()) {
        scope.launch { backgroundState = BackgroundSleepSync.refresh(context) }
    }
    fun enableBackgroundSync() {
        scope.launch {
            val state = BackgroundSleepSync.refresh(context)
            backgroundState = state
            if (state.access == BackgroundSleepSync.Access.BACKGROUND_PERMISSION_REQUIRED) {
                backgroundPermissionLauncher.launch(HealthConnectSleep.BACKGROUND_PERMISSIONS)
            }
        }
    }
    fun importFromHealthConnect() {
        if (hcBusy || loading) return
        hcBusy = true; hcMessage = null; foregroundSyncFailed = false
        scope.launch {
            val res = withContext(Dispatchers.IO) {
                try { Result.success(HealthConnectSleep.readAsCsv(context)) } catch (e: Exception) { Result.failure<HealthConnectSleep.ImportResult>(e) }
            }
            res.onSuccess { r ->
                healthConnected = true
                lastForegroundCheck = System.currentTimeMillis()
                if (r.sessionCount == 0) {
                    hcMessage = "수면 기록을 찾지 못했어요. 삼성 헬스 → 설정 → 헬스 커넥트 기능이 허용돼 있는지 확인해 주세요."
                    if (SleepDataStore.hasAny(dataDir)) analyzeStored(preserveForm = true)
                    else {
                        sourceName = "내 수면 기록"
                        fileReady = uploaded.isFile
                        runAnalysis(appliedInput, appliedCondition, appliedSurvey, preserveSelection = true)
                    }
                } else {
                    hcMessage = "수면 기록 ${r.sessionCount}건을 읽었어요 · ${AnalysisPresenter.dayTime(System.currentTimeMillis())} 확인" +
                        (r.lastMs?.let { "\n이번에 받은 마지막 수면 종료 ${AnalysisPresenter.dayTime(it)}" } ?: "")
                    analyzeStored(preserveForm = true) {
                        SleepDataStore.saveHealthConnect(dataDir, r.csv, source = if (r.fromSamsung) "samsung_health" else "health_connect")
                    }
                }
            }.onFailure { e ->
                foregroundSyncFailed = true
                if (e is SecurityException) healthConnected = false
                hcMessage = if (e is SecurityException) "수면 읽기 권한이 없어요. 다시 \"삼성 헬스에서 불러오기\"를 눌러 허용해 주세요."
                    else "헬스 커넥트에서 읽지 못했어요: ${e.message ?: e::class.java.simpleName}"
                if (SleepDataStore.hasAny(dataDir)) analyzeStored(preserveForm = true)
                else {
                    sourceName = "내 수면 기록"
                    fileReady = uploaded.isFile
                    runAnalysis(appliedInput, appliedCondition, appliedSurvey, preserveSelection = true)
                }
            }
            hcBusy = false
        }
    }
    val hcPermissionLauncher = rememberLauncherForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
        healthConnected = granted.containsAll(HealthConnectSleep.PERMISSIONS)
        if (healthConnected == true) importFromHealthConnect()
        else hcMessage = "수면 읽기를 허용해야 삼성 헬스 기록을 불러올 수 있어요."
        scope.launch { backgroundState = BackgroundSleepSync.refresh(context) }
    }
    fun connectHealthConnect() {
        when (HealthConnectSleep.availability(context)) {
            HealthConnectSleep.Availability.UNAVAILABLE ->
                hcMessage = "이 폰에서는 헬스 커넥트를 쓸 수 없어요. 안드로이드 13 이하는 Play 스토어에서 '헬스 커넥트(Health Connect)'를 설치해 주세요."
            HealthConnectSleep.Availability.NEEDS_UPDATE ->
                hcMessage = "헬스 커넥트 업데이트가 필요해요. Play 스토어에서 업데이트한 뒤 다시 눌러 주세요."
            HealthConnectSleep.Availability.AVAILABLE -> scope.launch {
                val ok = try { HealthConnectSleep.hasPermission(context) } catch (e: Exception) { false }
                healthConnected = ok
                if (ok) importFromHealthConnect() else hcPermissionLauncher.launch(HealthConnectSleep.PERMISSIONS)
            }
        }
    }
    // Foreground reads remain available without the optional background permission.
    val automaticRefresh by rememberUpdatedState(newValue = {
        if (inputPrepared && !hcBusy && !loading) scope.launch {
            val ok = HealthConnectSleep.availability(context) == HealthConnectSleep.Availability.AVAILABLE &&
                (try { HealthConnectSleep.hasPermission(context) } catch (_: Exception) { false })
            healthConnected = ok
            if (ok) importFromHealthConnect()
            else if (computed == null && SleepDataStore.hasAny(dataDir)) analyzeStored(preserveForm = true)
            else if (computed == null) { sourceName = "내 수면 기록"; runAnalysis(null, null) }
        }
        Unit
    })
    val lifecycleOwner = context as? LifecycleOwner
    LaunchedEffect(lifecycleOwner, inputPrepared) {
        if (!inputPrepared) return@LaunchedEffect
        if (lifecycleOwner == null) {
            backgroundState = BackgroundSleepSync.refresh(context)
            automaticRefresh()
        }
        else lifecycleOwner.lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            while (true) {
                backgroundState = BackgroundSleepSync.refresh(context)
                automaticRefresh()
                delay(15 * 60_000L)
            }
        }
    }
    val liveFormNight = NextNightModel.liveNightId(
        Instant.ofEpochMilli(nowMs).atZone(java.time.ZoneId.of(zone.zoneId)), profile.usualWake).toString()
    LaunchedEffect(importedNights, liveFormNight, zone, restoreRevision, form.sleepWakeDate) {
        run {
            val chosenSleep = form.sleepWakeDate ?: if (!manualEntry && !formDirty) importedNights.maxByOrNull { it.wakeTime }
                ?.takeIf { nowMs - Instant.parse(it.wakeTime).toEpochMilli() in 0..(36 * 3_600_000L) }?.day else null
            val loaded = withContext(Dispatchers.IO) {
                SurveyFormRestore.restore(SurveyStore.load(surveyFile), "me", Instant.ofEpochMilli(nowMs),
                    java.time.ZoneId.of(zone.zoneId), chosenSleep, profile.usualWake)
            }
            restoredSurvey = loaded
            if (!formDirty) {
                form = loaded.form
                formZoneId = zone.zoneId
            }
        }
    }

    // 지난 날 계산 결과 저장 (같은 날을 다시 고르면 다시 계산하지 않음). 새로 계산(파일·직접 입력)하면 비운다.
    val nightCache = remember(computed, zone) { mutableMapOf<Long, Pair<HealthResult<AnalysisResult>, ModelInputs>>() }

    // (2026-10-24 A안) 고른 날·표시 시간대가 바뀌면:
    //  - 오늘 → 현재 시각 기준으로 권장 갱신. 최근 기록이 있으면 그래프도 현재까지 계산
    //  - 지난 날     → 그날 아침 기상 시각(없으면 낮 12시)을 기준 시각으로 한 번 다시 계산
    //                  + 그날 설문 기록에 알람이 있으면 목표 기상으로 넣어 권장까지 계산
    // Do not cancel a native offline inference at each minute tick: a cold run can take minutes.
    var liveRefreshTick by remember { mutableStateOf(0L) }
    LaunchedEffect(Unit) {
        while (true) {
            kotlinx.coroutines.delay(60_000)
            if (!nightLoading && !loading) liveRefreshTick++
        }
    }
    LaunchedEffect(computed, selectedNight, zone, liveRefreshTick) {
        val c = computed ?: return@LaunchedEffect
        val calculationLease = try { c.input?.let { SavedSnapshot.retainCapturedInput(context.filesDir, it) } }
            catch (e: Exception) { nightError = Notice("계산 파일을 열지 못했어요", listOf(e.message.orEmpty())); return@LaunchedEffect }
        try {
            displayedCalculation = null
            val base = c.result
            val latestRef = nowMs
            val dataStart = listOfNotNull(base.sleepRecords.minOfOrNull { it.startMs }, base.scHistory.firstOrNull()?.timestampMs).minOrNull()
            val days = AnalysisPresenter.days(base.sleepRecords, c.direct, latestRef, dataStart)
            val sel = selectedNight?.let { k -> AnalysisPresenter.dayStartOf(k).let { st -> days.firstOrNull { it.startMs == st } } }
            nightError = null
            nightLoading = true
            if (sel == null || sel.isLatest) {
                val who = "me"
                val observedSurvey = withContext(Dispatchers.IO) {
                    val saved = try { SurveyStore.load(surveyFile) } catch (_: Exception) { emptyList() }
                    SurveyObservationPolicy.currentNight(saved, who, Instant.ofEpochMilli(nowMs), profile.usualWake,
                        java.time.ZoneId.of(zone.zoneId))
                }
                val liveNightId = NextNightModel.liveNightId(Instant.ofEpochMilli(nowMs).atZone(java.time.ZoneId.of(zone.zoneId)), profile.usualWake).toString()
                // A just-entered transient survey remains usable if persistence failed; persisted revisions always
                // win and are accepted only when their real savedAt is not in the future.
                val liveSurvey = observedSurvey?.survey ?: c.survey?.takeIf { it.date == liveNightId }
                val alarmMs = liveSurvey?.nextAlarmAfter(Instant.ofEpochMilli(nowMs))?.toEpochMilli()
                val liveDirect = if (alarmMs != null) (c.direct ?: DirectInput(null, null, null, null)).copy(targetWakeMs = alarmMs)
                    else c.direct?.copy(targetWakeMs = c.direct.targetWakeMs?.takeIf { it > nowMs })
                val liveComputed = withContext(Dispatchers.IO) {
                    analysisMutex.withLock {
                        val live = AnalysisRunner.runCurrent(
                            c.input?.file ?: uploaded, base, liveDirect, liveSurvey?.featureValues() ?: emptyMap(), nowMs,
                            contextObservedAtMs = observedSurvey?.observedAt?.toEpochMilli()
                        )
                        Computed(live, liveDirect, liveSurvey?.fatigue, liveSurvey, c.input, ModelInputs.capture(), c.source)
                    }
                }
                display = AnalysisPresenter.build(liveComputed.result, null, direct = liveDirect, condition = liveSurvey?.fatigue,
                    latestRefMs = latestRef, dataStartMs = dataStart, survey = liveSurvey, recommendationReferenceMs = nowMs)
                displayedCalculation = DisplayedCalculation(liveComputed, null, null, latestRef, dataStart, nowMs)
                displayedSurvey = liveSurvey
                return@LaunchedEffect
            }
            nightLoading = true
            val who = "me"
            val allSurveys = withContext(Dispatchers.IO) {
                try { SurveyStore.load(surveyFile) } catch (e: Exception) { emptyList<SurveyDay>() }
            }.filter { it.participant == who }
            val evaluationZone = java.time.ZoneId.of(zone.zoneId)
            val historicalNightId = SurveyCsv.surveyDateFor(sel.basisMs, zone.tz)
            val historicalCutoffMs = SurveyObservationPolicy.historicalCutoffMs(historicalNightId, evaluationZone)
            val csvDateCutoffMs = app.sleepwell.analysis.HistoricalCsvReanalysis.dataCutoffMs(
                sel.basisMs, java.time.ZoneId.of(zone.zoneId), historicalCutoffMs)
            val csvReanalysis = reanalysisCandidates.any {
                app.sleepwell.analysis.HistoricalCsvReanalysis.prepare(it, Instant.ofEpochMilli(csvDateCutoffMs)) != null
            }
            val inputCutoffMs = if (csvReanalysis) csvDateCutoffMs else historicalCutoffMs
            // A later answer must not leak across either the model cutoff or the selected display date.
            val historicalSurveys = if (csvReanalysis) allSurveys.filter { row ->
                row.savedAt?.let { runCatching { Instant.parse(it).toEpochMilli() < inputCutoffMs }.getOrDefault(false) } == true
            } else allSurveys
            val observedSurvey = SurveyObservationPolicy.historicalNight(historicalSurveys, who, historicalNightId, evaluationZone)
            val daySurvey = observedSurvey?.survey
            displayedSurvey = daySurvey
            val target = daySurvey?.nextAlarmAfter(Instant.ofEpochMilli(sel.basisMs + DirectInputParser.MIN_WAKE_GAP_MS))?.toEpochMilli()
            // Never copy today's direct sleep interval into a past reconstruction. Only the causally known
            // historical schedule constraint is eligible here; persisted diary rows keep their own modifiedAt.
            val pastDirect = target?.let { DirectInput(null, null, it, null) }
            // (임시) 수면시간 목표를 그날 기상까지의 기록으로 — 엔진은 파일 전체로 세서 미래 기록이 섞임 (AnalysisRunner.sleepTargetUntil 참고)
            val sleepTarget = AnalysisRunner.sleepTargetUntil(base.sleepRecords, sel.basisMs)
            val observedAtMs = observedSurvey?.observedAt?.toEpochMilli()
            val (res, pastModelInputs) = nightCache[sel.startMs] ?: withContext(Dispatchers.IO) {
                analysisMutex.withLock {
                NextNightModel.surveyDays = allSurveys
                // Use the raw captured import list and only answers known before this date's cutoff.
                // Pass inputs explicitly; no temporary mutation of live imports during background sync.
                val historicalImports = ImportedSleepStore.withQuestionnairesBefore(
                    reanalysisCandidates, historicalSurveys, Instant.ofEpochMilli(inputCutoffMs))
                val inputs = ModelInputs.capture().copy(imports = historicalImports)
                val features = daySurvey?.featureValues() ?: emptyMap()
                val result = if (c.input != null) AnalysisRunner.runFileAt(
                    c.input.file, pastDirect, sel.basisMs, sleepTarget, features,
                    recommendationTimeMs = historicalCutoffMs,
                    contextObservedAtMs = observedAtMs,
                    historicalReconstruction = true,
                    historicalCsvReanalysis = csvReanalysis, historicalImports = historicalImports,
                    historicalDataCutoffMs = inputCutoffMs.takeIf { csvReanalysis }
                )
                else HealthResult.Success(
                    AnalysisRunner.runDiary(
                        pastDirect, features, historicalCutoffMs,
                        contextObservedAtMs = observedAtMs,
                        historicalReconstruction = true,
                        historicalCsvReanalysis = csvReanalysis, historicalImports = historicalImports,
                        historicalDataCutoffMs = inputCutoffMs.takeIf { csvReanalysis }
                    )
                )
                    result to inputs
                }
            }.also { nightCache[sel.startMs] = it }
            display = withContext(Dispatchers.Default) {
                if (res is HealthResult.Success)
                    AnalysisPresenter.build(res.data, sel.startMs, direct = pastDirect, latestRefMs = latestRef, dataStartMs = dataStart)
                else
                    // 계산이 안 되는 날: 날짜 고르기는 계속 쓸 수 있게 날짜 목록만 업로드 결과로 만든다
                    AnalysisPresenter.build(base.copy(recommendation = null), sel.startMs, direct = pastDirect,
                        latestRefMs = latestRef, dataStartMs = dataStart)
            }
            if (res is HealthResult.Success) {
                displayedCalculation = DisplayedCalculation(
                    Computed(res.data, pastDirect, daySurvey?.fatigue, daySurvey, c.input, pastModelInputs, c.source),
                    sel.startMs, sel.basisMs, latestRef, dataStart, null)
            }
            if (res !is HealthResult.Success) {
                nightError = AnalysisPresenter.failureNotice(res, pastDay = true)
            }
            nightLoading = false
        } finally {
            nightLoading = false
            SavedSnapshot.releaseCapturedInput(context.filesDir, calculationLease)
        }
    }

    fun saveDisplayedResult() {
        if (saving || loading || nightLoading) return
        val d = display ?: return
        val frozen = displayedCalculation ?: return
        val c = frozen.computed
        val inputs = c.modelInputs
        val profileText = "성별 ${inputs.profile.sex} · " +
            "희망 수면 ${inputs.profile.desiredSleepMinutes / 60}시간 ${inputs.profile.desiredSleepMinutes % 60}분 · " +
            "평소 ${inputs.profile.usualBed} 취침 / ${inputs.profile.usualWake} 기상"
        val sv = c.survey?.takeIf { it.hasItems() }?.summaryIn(java.time.ZoneId.of(zone.zoneId), includeAlarm = false)
        val row = ResultStore.fromDisplay(d, c.source, sv, profileText = profileText)
        if (row == null) { saveMessage = "저장할 일정이 없어요."; return }
        val saveLease = try { c.input?.let { SavedSnapshot.retainCapturedInput(context.filesDir, it) } }
            catch (e: Exception) { saveMessage = "계산 파일을 열지 못했어요: ${e.message}"; return }
        saving = true
        scope.launch(start = kotlinx.coroutines.CoroutineStart.UNDISPATCHED) {
            try {
                saveMessage = withContext(Dispatchers.IO) {
                    var snapshotCreated = false
                    try {
                        SavedSnapshot.save(context.filesDir, row.savedAtMs, c.result, c.direct, c.condition, c.survey,
                            c.input?.file, expectedInputSha256 = c.input?.sha256,
                            surveys = inputs.surveys, profile = inputs.profile, diaryRecords = inputs.diaries,
                            importedRecords = inputs.imports, wearablePersonalizationEnabled = inputs.wearableEnabled,
                            dayMs = frozen.dayMs, basisMs = frozen.basisMs, latestRefMs = frozen.latestRefMs,
                            dataStartMs = frozen.dataStartMs, recommendationReferenceMs = frozen.recommendationReferenceMs)
                        snapshotCreated = true
                        val (_, replaced) = ResultStore.saveReplacing(storeFile, row)
                        // The new row is already committed. Cleanup must never roll it back.
                        replaced.forEach { runCatching { SavedSnapshot.delete(context.filesDir, it) } }
                        "저장했어요. 기록 달력에서 당시 결과와 그래프를 확인할 수 있어요."
                    } catch (e: Exception) {
                        if (snapshotCreated) runCatching { SavedSnapshot.delete(context.filesDir, row.savedAtMs) }
                        "저장하지 못했어요: ${e.message}"
                    }
                }
            } finally {
                SavedSnapshot.releaseCapturedInput(context.filesDir, saveLease)
                saving = false
            }
        }
    }

    // 달력에서 날짜 고르기. (A안) 고른 날짜 = 그날 아침 기상 기준
    fun openDatePicker(d: AnalysisDisplay) {
        val nights = d.nights
        val cur = d.night ?: return
        if (nights.isEmpty()) return
        val tz = zone.tz
        val c = Calendar.getInstance(tz).apply { timeInMillis = cur.startMs }
        val dlg = android.app.DatePickerDialog(context, { _, y, m, day ->
            val pick = Calendar.getInstance(tz).apply { clear(); set(y, m, day, 0, 0, 0) }.timeInMillis
            val target = nights.firstOrNull { it.startMs == pick }
            if (target != null) { selectedNight = target.startMs; pickMessage = null }
            else pickMessage = "그 날짜는 계산 범위 밖이에요. ${nights.first().label} ~ ${nights.last().label} 중에서 골라 주세요."
        }, c.get(Calendar.YEAR), c.get(Calendar.MONTH), c.get(Calendar.DAY_OF_MONTH))
        // 달력은 기기 시간대로 날짜를 그리므로, 고를 수 있는 범위를 같은 날짜의 기기 시간대 시각으로 바꿔서 건다
        fun deviceDay(ms: Long, endOfDay: Boolean): Long {
            val src = Calendar.getInstance(tz).apply { timeInMillis = ms }
            return Calendar.getInstance(tz).apply {
                clear()
                set(src.get(Calendar.YEAR), src.get(Calendar.MONTH), src.get(Calendar.DAY_OF_MONTH),
                    if (endOfDay) 23 else 0, if (endOfDay) 59 else 0, 0)
            }.timeInMillis
        }
        dlg.datePicker.minDate = deviceDay(nights.first().startMs, false)
        dlg.datePicker.maxDate = deviceDay(nights.last().startMs, true)
        dlg.show()
    }

    Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("SleepWell", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                Text(AnalysisPresenter.dayTime(nowMs), style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE)
                Text("시간대 자동 · ${zone.zoneId}", style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
            }
            TextButton(onClick = { showConnection = true }) { Text("연결·데이터") }
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("오늘의 수면 준비", style = MaterialTheme.typography.titleMedium)
                Text(if (importedNights.isEmpty()) "목표 기상과 오늘 컨디션을 필요할 때 입력해 주세요."
                    else "불러온 수면에 개운함·피로도를 더해 주세요.\n측정하지 못한 항목만 보완할 수 있어요.",
                    style = MaterialTheme.typography.bodyMedium)
                Button(onClick = { showInput = true }, enabled = !loading, modifier = Modifier.fillMaxWidth()) { Text("목표·컨디션 입력") }
                surveyNote?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE) }
            }
        }
        ConnectionStatusCard(healthConnected, hcBusy, foregroundSyncFailed ||
            backgroundState?.lastOutcome == BackgroundSleepSync.Outcome.RETRY.name,
            historyNights.size, historyNights.firstOrNull()?.wakeMs, lastForegroundCheck, nowMs,
            java.time.ZoneId.of(zone.zoneId), backgroundState, onOpen = { showConnection = true })
        LatestSleepCard(historyNights, java.time.ZoneId.of(zone.zoneId),
            onConnect = { showConnection = true }, nowMs = nowMs)
        if (loading || nightLoading) Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(Modifier.height(24.dp).width(24.dp))
            Text("  수면 계획 계산 중…", style = MaterialTheme.typography.bodyMedium)
        }
        error?.let { InfoCard(it.title, it.lines, MaterialTheme.colorScheme.errorContainer) }
        display?.let { d ->
            if (historyNights.isEmpty()) Text("입력한 수면 습관 기준 · 기록이 쌓이면 개인화에 반영돼요.",
                style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE)
            if (selectedNight != null && d.night?.isLatest != true) TextButton(onClick = { selectedNight = null }) { Text("오늘의 권장으로 돌아가기") }
            d.schedule?.let {
                ScheduleCard(it, onAskTargetWake = { showInput = true })
                OutlinedButton(onClick = { saveDisplayedResult() },
                    enabled = !saving && !loading && !nightLoading && displayedCalculation != null,
                    modifier = Modifier.fillMaxWidth()) { Text(if (saving) "저장 중…" else "이 결과 저장") }
                saveMessage?.let { message -> Text(message, style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE) }
            }
        }
        SleepTrendCard(historyNights, java.time.ZoneId.of(zone.zoneId), nowMs)
        if (display?.night?.isLatest == false) Text("과거 분석 · ${display?.night?.label} · 현재 상태와 구분해 확인해 주세요.",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.primary)
        ModelRhythmCard(display, nowMs)

        OutlinedButton(onClick = { showDetails = !showDetails; if (!showDetails) selectedNight = null }, modifier = Modifier.fillMaxWidth()) {
            Text(if (showDetails) "추천 근거 접기" else "추천 근거 · 과거 분석 보기")
        }
        if (showDetails) display?.let { d ->
            NightSelector(d, zone, pickMessage, onSelect = { selectedNight = it; pickMessage = null }, onPickDate = { openDatePicker(d) })
            if (selectedNight == null) computed?.result?.recommendation?.let { rec ->
                rec.generatedAtMs?.let { Text("추천 갱신 ${AnalysisPresenter.dayTime(it)}", style = MaterialTheme.typography.bodySmall) }
                rec.personalizationStatus?.let { Text("개인화 상태 · $it", style = MaterialTheme.typography.bodySmall) }
                rec.residualModelVersion?.let { Text("모델 · $it", style = MaterialTheme.typography.bodySmall) }
            }
            Text("워치 수면 ${importedNights.size}개 · 직접 일지 ${diaryCount}개 · 같은 밤은 일지 우선으로 하이브리드 예측·개인화에 반영해요.",
                style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE)
            when {
                nightLoading -> CircularProgressIndicator()
                nightError != null -> nightError?.let { InfoCard(it.title, it.lines, MaterialTheme.colorScheme.errorContainer) }
                else -> AnalysisContent(d, sourceName.orEmpty(), zone, onAskTargetWake = { showInput = true },
                    onSave = null, saveMessage = saveMessage, showSchedule = false)
            }
        }
        Spacer(Modifier.height(20.dp))
    }
    if (showInput) ModalBottomSheet(onDismissRequest = { showInput = false },
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp)) {
            TextButton(onClick = { showInput = false }) { Text("닫기 · 작성 중인 내용 유지") }
            if (formZoneId != zone.zoneId && form != DirectInputForm()) Text("작성 중인 시각은 $formZoneId 기준입니다.", style = MaterialTheme.typography.bodySmall)
            DirectInputCard(importedNights, lastRecordEnd, form, onChange = { changed ->
                if (changed.sleepWakeDate != form.sleepWakeDate) {
                    // Selecting another completed sleep restores only that sleep's response. Keep the current-night draft.
                    val restored = SurveyFormRestore.restore(SurveyStore.load(surveyFile), "me", Instant.ofEpochMilli(nowMs),
                        java.time.ZoneId.of(formZoneId), changed.sleepWakeDate, profile.usualWake)
                    form = changed.copy(condition = restored.form.condition, rested = restored.form.rested,
                        sleepLatencyText = restored.form.sleepLatencyText, awakeMinutesText = restored.form.awakeMinutesText,
                        exerciseMinText = restored.form.exerciseMinText, exerciseIntensity = restored.form.exerciseIntensity,
                        fatigueScaleMax = restored.form.fatigueScaleMax)
                    restoredSurvey = restored.copy(context = restoredSurvey?.context ?: restored.context)
                } else form = changed
                formDirty = true
            }, errors = formErrors,
                enabled = !loading, recommendationReady = NextNightModel.isInstalled, savedNote = surveyNote,
                manualEntry = manualEntry, onManualEntry = { manualEntry = true }, onApply = { applyForm() })
            Spacer(Modifier.height(24.dp))
        }
    }
    if (showConnection) ModalBottomSheet(onDismissRequest = { showConnection = false },
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("연결·데이터", style = MaterialTheme.typography.headlineSmall)
            Text("삼성 헬스가 헬스 커넥트에 공유한 수면을 먼저 불러옵니다. 앱 진입·복귀와 사용 중 15분마다 새 기록을 확인해요.", style = MaterialTheme.typography.bodyMedium)
            Text("현재 수면·각성 상태: 이 연결 경로에서는 제공되지 않아요.", style = MaterialTheme.typography.bodyMedium)
            Text("수면이 끝난 뒤 공유된 기록을 반영합니다. 새 기록 확인 시각은 워치의 실시간 측정 시각과 달라요.", style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE)
            Button(onClick = { connectHealthConnect() }, enabled = !hcBusy && !loading, modifier = Modifier.fillMaxWidth()) {
                Text(if (hcBusy) "불러오는 중…" else if (healthConnected == true) "지금 새 기록 확인" else "삼성 헬스 연결하기")
            }
            hcMessage?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            lastForegroundCheck?.let { Text("마지막 확인 ${AnalysisPresenter.dayTime(it)}", style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE) }
        backgroundState?.let { state ->
            val backgroundText = when (state.access) {
                BackgroundSleepSync.Access.READY -> if (state.scheduled)
                    "앱을 닫아도 자동 누적이 켜져 있어요. 약 1시간 간격으로 확인하며 배터리·시스템 상태에 따라 늦어질 수 있어요."
                    else "백그라운드 작업을 예약하지 못했어요. 다시 시도해 주세요."
                BackgroundSleepSync.Access.BACKGROUND_PERMISSION_REQUIRED -> "앱을 닫아 둔 동안에도 누적하려면 백그라운드 수면 읽기를 추가로 허용해 주세요."
                BackgroundSleepSync.Access.SLEEP_PERMISSION_REQUIRED -> "삼성 헬스를 먼저 연결하면 백그라운드 자동 누적을 설정할 수 있어요."
                BackgroundSleepSync.Access.UNSUPPORTED -> "이 기기의 헬스 커넥트는 백그라운드 읽기를 지원하지 않아요. 앱을 열면 자동으로 불러옵니다."
                BackgroundSleepSync.Access.PROVIDER_UNAVAILABLE -> "헬스 커넥트 설치·업데이트 후 백그라운드 자동 누적을 설정할 수 있어요."
                BackgroundSleepSync.Access.CHECK_FAILED -> "백그라운드 연결 상태를 확인하지 못했어요. 다시 시도해 주세요."
            }
            Text(backgroundText, style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
            if (state.lastSuccessMs > 0) Text("마지막 백그라운드 확인 ${AnalysisPresenter.dayTime(state.lastSuccessMs)}",
                style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
            if (state.lastOutcome == BackgroundSleepSync.Outcome.RETRY.name) Text("최근 백그라운드 확인이 실패해 다음 가능한 시점에 다시 시도합니다. 저장된 기록은 유지돼요.",
                style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
            if (state.access == BackgroundSleepSync.Access.BACKGROUND_PERMISSION_REQUIRED ||
                state.access == BackgroundSleepSync.Access.CHECK_FAILED ||
                (state.access == BackgroundSleepSync.Access.READY && !state.scheduled)) {
                OutlinedButton(onClick = { enableBackgroundSync() }, enabled = !hcBusy && !loading,
                    modifier = Modifier.fillMaxWidth()) {
                    Text(if (state.access == BackgroundSleepSync.Access.BACKGROUND_PERMISSION_REQUIRED)
                        "앱을 닫아도 자동 누적 허용" else "백그라운드 연결 다시 확인")
                }
            }
            if (state.scheduled) Text("설정에서 앱을 강제 종료하면 다시 열 때까지 동기화가 멈춥니다. 백그라운드 읽기 허용은 헬스 커넥트 앱 권한에서 끌 수 있어요.",
                style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
        }


            Text("선택해서 추가하기", style = MaterialTheme.typography.titleMedium)
            Text("필요할 때만 파일이나 직접 기록을 추가할 수 있어요.", style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE)
            OutlinedButton(onClick = { showCsvGuide = true }, enabled = !loading, modifier = Modifier.fillMaxWidth()) { Text("삼성 헬스 내보내기 파일 추가") }
            folderMessage?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            OutlinedButton(onClick = {
                manualEntry = true
                form = form.copy(sleepWakeDate = null, condition = null, rested = null, sleepLatencyText = "", awakeMinutesText = "")
                showConnection = false; showInput = true
            }, enabled = !loading, modifier = Modifier.fillMaxWidth()) { Text("수면일지 직접 작성") }
            ProfileCard(profile, profileSaved, !loading) { p ->
                try {
                    ProfileStore.save(profileFile, p); profileSaved = true; profileStoreWarning = null
                    profile = p; NextNightModel.profile = p; runAnalysis(appliedInput, appliedCondition, appliedSurvey); null
                } catch (e: Exception) { "내 정보를 저장하지 못했어요: ${e.message}" }
            }
            profileStoreWarning?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            TextButton(onClick = { showConnection = false }) { Text("닫기") }
            Spacer(Modifier.height(24.dp))
        }
    }
        if (showCsvGuide) {
            AlertDialog(
                onDismissRequest = { showCsvGuide = false },
                title = { Text("불러올 폴더를 선택해 주세요.") },
                text = {
                    Column {
                        // 긴 파일 이름은 통째로 다음 줄로 넘어가 첫 줄 끝이 크게 비었다 → 글자 사이에 줄바꿈 가능 표시(폭 0 공백)를 넣고,
                        // 줄을 고르게 나누는 기본 방식 대신 한 줄을 끝까지 채우는 방식(LineBreak.Simple)으로 → 한 문단처럼 보이게
                        Text(
                            "'다운로드 > Samsung Health' 폴더를 고르면, 그 안에서 수면 단계 파일(" + breakAnywhere("com.samsung.health.sleep_stage…csv") + ")을 찾아 읽어요.",
                            style = MaterialTheme.typography.bodyMedium.copy(lineBreak = LineBreak.Simple)
                        )
                        Spacer(Modifier.height(8.dp))
                        listOf(
                            "Samsung Health 폴더로 들어간 뒤, 아래 '이 폴더 사용'을 눌러 주세요.",
                            "파일 하나를 직접 고르고 싶으면, 아래 '파일 직접 고르기'를 선택해 주세요."
                        ).forEach { line ->
                            Row {
                                Text("·  ", style = MaterialTheme.typography.bodySmall, color = COLOR_NOTE)
                                Text(line, style = MaterialTheme.typography.bodySmall.copy(lineBreak = LineBreak.Simple), color = COLOR_NOTE)
                            }
                        }
                    }
                },
                confirmButton = {
                    TextButton(onClick = {
                        showCsvGuide = false
                        // 폴더 고르는 화면(안드로이드 기본 화면)에는 글을 넣을 수 없어서, 열리는 동안 같은 안내를 잠깐 띄운다
                        android.widget.Toast.makeText(context, "삼성 헬스 내보내기 폴더로 들어가서 '이 폴더 사용'을 눌러 주세요", android.widget.Toast.LENGTH_LONG).show()
                        folderPicker.launch(null)
                    }) { Text("폴더 고르기") }
                },
                dismissButton = {
                    Row {
                        TextButton(onClick = {
                            showCsvGuide = false
                            android.widget.Toast.makeText(context, "이름에 'stage'가 있는 파일을 골라 주세요", android.widget.Toast.LENGTH_LONG).show()
                            picker.launch(arrayOf("text/*", "text/csv", "text/comma-separated-values", "application/octet-stream"))
                        }) { Text("파일 직접 고르기") }
                        TextButton(onClick = { showCsvGuide = false }) { Text("취소") }
                    }
                }
            )
        }

}

/**
 * 날짜 고르기 (2026-10-24 A안): ◀ [10/20(화) 기상] ▶ + 달력 + (지난 날이면) 가장 최근으로
 */
@Composable
@Suppress("UNUSED_PARAMETER")   // zone: 시간대를 바꾸면 다시 그리게 하려고 받는다
private fun NightSelector(
    d: AnalysisDisplay,
    zone: DisplayZone,
    message: String?,
    onSelect: (Long) -> Unit,
    onPickDate: () -> Unit
) {
    val night = d.night ?: return
    val idx = d.nights.indexOfFirst { it.startMs == night.startMs }
    val prev = d.nights.getOrNull(idx - 1)
    val next = d.nights.getOrNull(idx + 1)
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = { prev?.let { onSelect(it.startMs) } }, enabled = prev != null) { Text("◀") }
                Text(
                    night.label, style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f), textAlign = TextAlign.Center
                )
                OutlinedButton(onClick = { next?.let { onSelect(it.startMs) } }, enabled = next != null) { Text("▶") }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                TextButton(onClick = onPickDate) { Text("날짜 고르기") }
                if (!night.isLatest) TextButton(onClick = { onSelect(d.nights.last().startMs) }) { Text("오늘로") }
            }
            message?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error) }
        }
    }
}

@Composable
internal fun AnalysisContent(
    d: AnalysisDisplay, sourceName: String, zone: DisplayZone, onAskTargetWake: () -> Unit,
    /** (2026-10-24) "이 결과 저장" — 가장 최근 날의 일정 카드 아래에만 */
    onSave: (() -> Unit)? = null,
    saveMessage: String? = null,
    showSchedule: Boolean = true
) {
    val latest = d.night?.isLatest != false
    // 위쪽 안내: 임시 모델 · 데이터 · (기록이 적을 때만) 주의 한 줄
    SoftBox {
        Text(
            (if (d.isTemporaryModel) "임시 모델로 계산했어요 · " else "") + "데이터: $sourceName",
            style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE
        )
        val caution = when {
            d.history.isEmpty() -> null
            d.night?.isPartial == true -> "기록이 시작된 직후라 수면 압력이 실제와 다를 수 있어요."
            d.windowSleepCount < 3 -> "앞 7일 기록이 ${d.windowSleepCount}번뿐이라, 기록 없는 날이 깨어 있던 것으로 계산돼 수면 압력이 높게 나올 수 있어요."
            else -> null
        }
        caution?.let { Text("⚠ $it", style = MaterialTheme.typography.labelSmall, color = Color(0xFF8A5A00)) }
    }
    if (latest && d.inputNotes.isNotEmpty()) {
        Spacer(Modifier.height(8.dp))
        InfoCard("입력 반영", d.inputNotes, Color(0xFFE8F0FB))
    }

    // The visible home chart owns graphical presentation; detail keeps interpretation and saved results.
    d.analysisLines.forEach { line ->
        Spacer(Modifier.height(12.dp))
        Text(line.title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
        Text(segText(line.segs), style = MaterialTheme.typography.bodyMedium)
    }

    // ── 예측 및 권장: 그날 밤 수면 일정 ──
    d.schedule?.let {
        Spacer(Modifier.height(16.dp))
        if (showSchedule) ScheduleCard(it, onAskTargetWake)
        // 결과 저장 — 가장 최근 날만 (흐름: 분석 → 권장 → 저장·조회)
        if (onSave != null) {
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = onSave, modifier = Modifier.fillMaxWidth()) { Text("이 결과 저장") }
            saveMessage?.let { m -> Text(m, style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE) }
        }
    }

    if (!latest) return     // 최근 기록 목록은 가장 최근 날에서만

    Spacer(Modifier.height(16.dp))
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("최근 수면 단계 기록", style = MaterialTheme.typography.titleMedium)
            if (d.recentSleeps.isEmpty()) Text("수면 단계 자료가 없어요. 수면 시각만 있는 기록은 위 예측과 누적 기록에서 확인해 주세요.", style = MaterialTheme.typography.bodySmall)
            d.recentSleeps.forEach { SleepRecordRow(it, zone) }
        }
    }
}

/** 해석 문장 조각 → 색·굵기·노란 바탕 (2026-10-24) */
private fun segText(segs: List<Seg>) = buildAnnotatedString {
    segs.forEach { sg ->
        val st = when (sg.style) {
            SegStyle.PLAIN -> null
            SegStyle.BOLD -> SpanStyle(fontWeight = FontWeight.Bold)
            SegStyle.S -> SpanStyle(color = COLOR_S, fontWeight = FontWeight.Bold)
            SegStyle.C -> SpanStyle(color = COLOR_C, fontWeight = FontWeight.Bold)
            SegStyle.MEANING -> SpanStyle(background = COLOR_MEANING, color = Color(0xFF1F2933))
            SegStyle.MEANING_BOLD -> SpanStyle(background = COLOR_MEANING, color = Color(0xFF1F2933), fontWeight = FontWeight.Bold)
            SegStyle.SLEEP_SWATCH -> SpanStyle(color = COLOR_SLEEP_SWATCH)
        }
        if (st == null) append(sg.text) else withStyle(st) { append(sg.text) }
    }
}

@Composable
@Suppress("UNUSED_PARAMETER")   // zone: 시간대를 바꾸면 이 줄도 다시 그리게 하려고 받는다 (글자는 AnalysisPresenter 가 표시 시간대로 만든다)
private fun SleepRecordRow(r: SleepRecord, zone: DisplayZone) {
    val total = r.totalSleepMin
    val parts = listOf("LIGHT", "DEEP", "REM").joinToString(" · ") { k ->
        val m = r.stageSummary[k] ?: 0.0
        val pct = if (total > 0) m * 100 / total else 0.0
        "$k ${pct.toInt()}%"
    }
    Spacer(Modifier.height(6.dp))
    Text(
        "${AnalysisPresenter.dayTime(r.startMs)} → ${AnalysisPresenter.hhmm(r.endMs)}",
        style = MaterialTheme.typography.labelLarge
    )
    Text(
        "잔 시간 ${(total / 60).toInt()}시간 ${(total % 60).toInt()}분 · $parts",
        style = MaterialTheme.typography.bodySmall
    )
}

@Composable
private fun Legend(label: String, color: Color) {
    Row {
        Canvas(Modifier.width(14.dp).height(10.dp)) {
            drawLine(color, Offset(0f, size.height / 2), Offset(size.width, size.height / 2), 3.dp.toPx())
        }
        Text(" $label", style = MaterialTheme.typography.labelSmall)
    }
}

/** 연회색 둥근 상자 (체크 설명·경향 문장) */
@Composable
private fun SoftBox(content: @Composable () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) { content() }
    }
}

private fun colorOf(k: ReadingKind) = when (k) { ReadingKind.S -> COLOR_S; ReadingKind.C -> COLOR_C; ReadingKind.P -> COLOR_P }

/** 지금 상태의 값 하나: 제목·큰 값 → (막대) → 설명 → (세부) → (작은 주석) */
@Composable
private fun ReadingBlock(r: Reading) {
    val color = colorOf(r.kind)
    Spacer(Modifier.height(14.dp))
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(r.title, style = MaterialTheme.typography.labelLarge, color = color)
        Text(r.value, style = MaterialTheme.typography.titleLarge, color = color, fontWeight = FontWeight.Bold)
    }
    r.gauge?.let { GaugeBar(it, color) }
    if (r.meaning.isNotBlank()) Text(r.meaning, style = MaterialTheme.typography.bodySmall)
    r.details.forEach { Text("· $it", style = MaterialTheme.typography.bodySmall) }
    r.note?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE) }
}

/** 범위 막대: 연한 색 → 진한 색, 지금 값 위치에 검은 표시 */
@Composable
private fun GaugeBar(g: Gauge, color: Color) {
    val markerColor = MaterialTheme.colorScheme.onSurface
    Spacer(Modifier.height(4.dp))
    Canvas(Modifier.fillMaxWidth().height(14.dp)) {
        val barH = 8.dp.toPx(); val top = (size.height - barH) / 2
        drawRoundRect(
            Brush.horizontalGradient(listOf(color.copy(alpha = 0.15f), color)),
            Offset(0f, top), Size(size.width, barH), CornerRadius(barH / 2)
        )
        val f = ((g.value - g.min) / (g.max - g.min)).coerceIn(0.0, 1.0).toFloat()
        val mx = (size.width * f).coerceIn(2.dp.toPx(), size.width - 2.dp.toPx())
        drawRoundRect(markerColor, Offset(mx - 2.dp.toPx(), 0f), Size(4.dp.toPx(), size.height), CornerRadius(2.dp.toPx()))
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(g.leftLabel, style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
        Text(g.rightLabel, style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun DirectInputCard(
    importedNights: List<app.sleepwell.user.SleepRecord>,
    lastRecordEnd: Long?,
    form: DirectInputForm,
    onChange: (DirectInputForm) -> Unit,
    errors: List<String>,
    enabled: Boolean,
    /** 권장 엔진이 설치되어 목표 기상 제약을 받을 수 있는가 */
    recommendationReady: Boolean,
    /** 설문 누적 저장 결과 한 줄 */
    savedNote: String?,
    manualEntry: Boolean,
    onManualEntry: () -> Unit,
    onApply: () -> Unit
) {
    // (2026-10-24) 직접 입력 → 「실제 컨디션 입력」 + 취침 전 설문 항목. 마지막 기록이 "오늘 아침"이라는 보장이 없어서 "오늘 밤"이라 부르지 않는다.
    //  계산에 실제로 쓰이는지 항목마다 꼬리표로 밝힌다.
    val context = LocalContext.current
    var chooseSleep by remember { mutableStateOf(false) }
    val linkedSleep = importedNights.firstOrNull { it.day == form.sleepWakeDate }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("목표·컨디션 입력", style = MaterialTheme.typography.titleMedium)
            lastRecordEnd?.let { last ->
                Text("마지막 기록: ${DirectInputParser.toText(last)}", style = MaterialTheme.typography.bodySmall, color = Color(0xFF3D6FB6))
            }
            Spacer(Modifier.height(10.dp))
            Text("수면 기록 연결", style = MaterialTheme.typography.titleSmall)
            Hint("삼성 헬스 연결 후 앱 진입·복귀와 화면 사용 중 15분마다 확인해요. 위에서 백그라운드 읽기도 허용하면 앱을 닫아도 자동 누적합니다.")
            if (importedNights.isNotEmpty()) {
                linkedSleep?.let { record ->
                    Text("설문 대상: ${DirectInputParser.toText(Instant.parse(record.bedtime).toEpochMilli())} ~ " +
                        DirectInputParser.toText(Instant.parse(record.wakeTime).toEpochMilli()),
                        style = MaterialTheme.typography.bodyMedium)
                    Hint("취침·기상은 불러온 기록을 사용합니다. 직접 일지를 다시 작성할 필요가 없어요.")
                    record.awakeMinutes?.let { Hint("워치에서 측정한 각성 시간 ${it}분이 있어요. 비어 있는 측정값만 아래 설문으로 보완합니다.") }
                    Unit
                } ?: Hint("개운함·피로도 등을 연결할 수면 기록을 선택해 주세요.")
                OutlinedButton(onClick = { chooseSleep = true }, enabled = enabled) { Text("보완할 수면 기록 선택") }
                if (linkedSleep != null) TextButton(onClick = {
                    onManualEntry()
                    onChange(form.copy(sleepWakeDate = null, condition = null, rested = null,
                        sleepLatencyText = "", awakeMinutesText = "", exerciseMinText = "", exerciseIntensity = null))
                }, enabled = enabled) { Text("연결 해제 · 직접 일지 작성") }
            } else Hint("수면 기록을 불러오면 개운함·피로도를 해당 수면에 연결할 수 있어요.")
            if (linkedSleep == null && !manualEntry) TextButton(onClick = onManualEntry) { Text("필요하면 수면일지 직접 작성") }
            if (chooseSleep) AlertDialog(onDismissRequest = { chooseSleep = false },
                title = { Text("어느 수면에 대한 설문인가요?") },
                text = {
                    Column(Modifier.verticalScroll(rememberScrollState())) {
                        importedNights.sortedByDescending { it.wakeTime }.forEach { record ->
                            TextButton(onClick = {
                                onChange(form.copy(sleepWakeDate = record.day, bedText = "", wakeText = "",
                                    condition = null, rested = null, sleepLatencyText = "", awakeMinutesText = "",
                                    exerciseMinText = "", exerciseIntensity = null))
                                chooseSleep = false
                            }) { Text("기상 ${DirectInputParser.toText(Instant.parse(record.wakeTime).toEpochMilli())}") }
                        }
                    }
                }, confirmButton = { TextButton(onClick = { chooseSleep = false }) { Text("닫기") } })

            // ── 목표 기상 ──
            Spacer(Modifier.height(10.dp))
            FieldTitle("목표 기상 시각", if (recommendationReady) StatusTag.USED else StatusTag.PREPARING)
            OutlinedTextField(
                value = form.targetWakeText, onValueChange = { onChange(form.copy(targetWakeText = it)) },
                label = { Text("목표 기상") }, placeholder = { Text("07:30") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                trailingIcon = {
                    IconButton(modifier = Modifier.semantics { contentDescription = "목표 기상 시각 선택" }, onClick = { pickTime(context, form.targetWakeText, 7, 0) { onChange(form.copy(targetWakeText = it)) } }) { Text("🕒") }
                }
            )

            Hint("평소보다 일찍 일어나야 하는 날에 적용해요. 평소 기상 시각 변경은 내 정보에서 해 주세요.")

            // ── 피로도 ──
            Spacer(Modifier.height(10.dp))
            FieldTitle("피로도", StatusTag.ACCUMULATES)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                (1..5).forEach { v ->
                    FilterChip(
                        selected = form.fatigueScaleMax == FatigueScale.CURRENT_MAX && form.condition == v,
                        onClick = { onChange(form.copy(
                            condition = if (form.fatigueScaleMax == FatigueScale.CURRENT_MAX && form.condition == v) null else v,
                            fatigueScaleMax = FatigueScale.CURRENT_MAX)) },
                        label = { Text("$v") }
                    )
                }
            }
            Hint("1 = 매우 맑음 · 3 = 보통 · 5 = 매우 피곤함")
            if (form.condition != null && form.fatigueScaleMax != FatigueScale.CURRENT_MAX)
                Hint("이전 초안의 ${FatigueScale.displayText(form.condition, form.fatigueScaleMax)}를 유지하고 있어요. 다시 선택하면 5점 척도로 저장해요.")

            // 개운함은 기상 직후 응답. 엔진의 기존 개인화 기준(개운함 >=4, 5점 피로도 <=3)은 유지한다.
            Spacer(Modifier.height(10.dp))
            FieldTitle("아침 개운함", StatusTag.ACCUMULATES)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                (1..5).forEach { v ->
                    FilterChip(
                        selected = form.rested == v,
                        onClick = { onChange(form.copy(rested = if (form.rested == v) null else v)) },
                        label = { Text("$v") }
                    )
                }
            }
            Hint("1 = 전혀 개운하지 않음 · 5 = 매우 개운함 (선택한 수면에서 일어났을 때)")
            Spacer(Modifier.height(10.dp))
            Text("수면 보완 설문", style = MaterialTheme.typography.titleSmall)
            Hint("워치에서 얻지 못한 값만 입력해 주세요. 모르면 비워 두고, 실제로 없었던 경우에만 0을 입력해 주세요.")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (linkedSleep?.sleepLatencyMinutes == null) NumberField("입면까지 (분)", form.sleepLatencyText, Modifier.weight(1f)) { onChange(form.copy(sleepLatencyText = it)) }
                if (linkedSleep?.awakeMinutes == null) NumberField("중간각성 (분)", form.awakeMinutesText, Modifier.weight(1f)) { onChange(form.copy(awakeMinutesText = it)) }
            }

            var showLifestyle by rememberSaveable { mutableStateOf(false) }
            TextButton(onClick = { showLifestyle = !showLifestyle }) { Text(if (showLifestyle) "카페인·낮잠·운동 접기" else "카페인·낮잠·운동 추가 (선택)") }
            if (showLifestyle) {
            // ── 카페인 ──
            Spacer(Modifier.height(10.dp))
            FieldTitle("카페인", StatusTag.RECORD_ONLY)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                NumberField("양 (mg)", form.caffeineMgText, Modifier.weight(1f)) { onChange(form.copy(caffeineMgText = it)) }
                TimeField("마지막 섭취", form.lastCaffeineText, 15, 0, Modifier.weight(1f)) { onChange(form.copy(lastCaffeineText = it)) }
            }
            Hint("안 마셨으면 0 · 아메리카노 한 잔 ≈ 100~150mg")

            // ── 낮잠 ──
            Spacer(Modifier.height(10.dp))
            FieldTitle("낮잠", StatusTag.COMPLETED_NAP)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                NumberField("시간 (분)", form.napMinText, Modifier.weight(1f)) { onChange(form.copy(napMinText = it)) }
                TimeField("끝난 시각", form.napEndText, 15, 0, Modifier.weight(1f)) { onChange(form.copy(napEndText = it)) }
            }

            // ── 운동 ──
            Spacer(Modifier.height(10.dp))
            FieldTitle("운동", StatusTag.DIARY_STATISTIC)
            Hint("운동 시간은 일지 통계에, 강도는 기록 보관에 사용해요. 권장 시각을 직접 바꾸지는 않아요.")
            NumberField("시간 (분)", form.exerciseMinText, Modifier.fillMaxWidth()) { onChange(form.copy(exerciseMinText = it)) }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                listOf("none" to "안 함", "light" to "가벼움", "moderate" to "보통", "vigorous" to "격함").forEach { (k, w) ->
                    FilterChip(
                        selected = form.exerciseIntensity == k,
                        onClick = { onChange(form.copy(exerciseIntensity = if (form.exerciseIntensity == k) null else k)) },
                        label = { Text(w) }
                    )
                }
            }

            }

            // ── 직접 일지는 워치가 없거나 명시적으로 연결을 해제한 경우에만 ──
            if (linkedSleep == null && manualEntry) {
            Spacer(Modifier.height(14.dp))
            FieldTitle("직접 수면일지", StatusTag.ACCUMULATES)
            Hint("워치 기록이 없는 날에 누운 시각과 최종 기상을 입력해 주세요. 위 수면 보완 설문도 함께 저장됩니다.")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = form.bedText, onValueChange = { onChange(form.copy(bedText = it)) },
                    label = { Text("누운 시각") }, placeholder = { Text("yyyy-MM-dd HH:mm") }, singleLine = true, modifier = Modifier.weight(1f)
                )
                OutlinedTextField(
                    value = form.wakeText, onValueChange = { onChange(form.copy(wakeText = it)) },
                    label = { Text("최종 기상") }, placeholder = { Text("yyyy-MM-dd HH:mm") }, singleLine = true, modifier = Modifier.weight(1f)
                )
            }
            Hint("0은 실제로 없었던 경우에만 입력해 주세요. 일지는 기기에 누적 저장합니다.")
            }

            if (errors.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                errors.forEach { Text("· $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error) }
            }
            Spacer(Modifier.height(8.dp))
            Button(onClick = onApply, enabled = enabled, modifier = Modifier.fillMaxWidth()) {
                Text("저장하고 다시 계산")
            }
            savedNote?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = Color(0xFF3D6FB6)) }
        }
    }
}

/** 저장된 기본정보. 최초 입력과 같은 편집기를 사용한다. */
@Composable
private fun ProfileCard(
    profile: NextNightModel.ModelProfile,
    saved: Boolean,
    enabled: Boolean,
    onSave: (NextNightModel.ModelProfile) -> String?
) {
    var editing by rememberSaveable { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("내 정보", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { editing = true }, enabled = enabled) { Text("수정") }
            }
            Text(ProfileStore.summary(profile) + if (saved) "" else " (기본값)",
                style = MaterialTheme.typography.bodySmall)
        }
    }
    if (editing) {
        Dialog(onDismissRequest = { editing = false },
            properties = DialogProperties(usePlatformDefaultWidth = false)) {
            Surface(Modifier.fillMaxSize()) {
                ProfileEditor(initialProfile = profile, firstRun = false,
                    onSave = { changed -> onSave(changed).also { if (it == null) editing = false } },
                    onCancel = { editing = false })
            }
        }
    }
}

/** 입력 항목이 계산에 실제로 쓰이는지 (2026-10-24, 작업 지시서 "개발 완료/개발 중 구분") */
private enum class StatusTag(val text: String, val fg: Color, val bg: Color) {
    USED("평소보다 이른 목표일 때 반영", Color(0xFF1E6B3D), Color(0xFFE6F4EA)),
    RECORD_ONLY("계산에 반영 안 됨 · 기록으로 쌓임", Color(0xFF6B4A00), Color(0xFFFFF4DC)),
    COMPLETED_NAP("✓ 완료된 낮잠은 수면압력에 반영", Color(0xFF1E6B3D), Color(0xFFE6F4EA)),
    DIARY_STATISTIC("수면 기록의 개인화 통계에 반영", Color(0xFF1E6B3D), Color(0xFFE6F4EA)),
    PREPARING("권장 모델 연결 확인 필요", Color(0xFF5A5F6B), Color(0xFFEDEFF2)),
    ACCUMULATES("✓ 수면 기록이 쌓이면 개인화", Color(0xFF1E6B3D), Color(0xFFE6F4EA))
}

@Composable
private fun FieldTitle(title: String, tag: StatusTag) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(title, style = MaterialTheme.typography.labelLarge, modifier = Modifier.weight(1f))
        Card(colors = CardDefaults.cardColors(containerColor = tag.bg)) {
            Text(tag.text, style = MaterialTheme.typography.labelSmall, color = tag.fg,
                modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp))
        }
    }
}

@Composable
private fun Hint(text: String) = Text(text, style = MaterialTheme.typography.labelSmall, color = COLOR_NOTE)

@Composable
private fun NumberField(label: String, value: String, modifier: Modifier, onValue: (String) -> Unit) {
    OutlinedTextField(
        value = value, onValueChange = onValue, label = { Text(label) }, singleLine = true, modifier = modifier,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
    )
}

@Composable
private fun TimeField(label: String, value: String, defH: Int, defM: Int, modifier: Modifier, onValue: (String) -> Unit) {
    val context = LocalContext.current
    OutlinedTextField(
        value = value, onValueChange = onValue, label = { Text(label) }, singleLine = true, modifier = modifier,
        trailingIcon = { IconButton(modifier = Modifier.semantics { contentDescription = "$label 시각 선택" }, onClick = { pickTime(context, value, defH, defM, onValue) }) { Text("🕒") } }
    )
}

/**
 * 알람 앱처럼 시·분을 고르는 창 (2026-10-24). 고른 값은 "HH:mm" 으로 칸에 들어가고,
 * 날짜는 DirectInputParser 가 기준 시각 다음에 오는 그 시각으로 정한다.
 * 칸에 이미 "07:30" 이나 "2026-09-16 07:30" 이 있으면 그 시각에서 시작한다.
 */
private fun pickTime(context: Context, current: String, defH: Int, defM: Int, onPick: (String) -> Unit) {
    val m = Regex("""(\d{1,2}):(\d{2})\s*$""").find(current.trim())
    val h0 = m?.groupValues?.get(1)?.toIntOrNull()?.takeIf { it in 0..23 } ?: defH
    val m0 = m?.groupValues?.get(2)?.toIntOrNull()?.takeIf { it in 0..59 } ?: defM
    android.app.TimePickerDialog(context, { _, h, mi ->
        onPick(String.format(java.util.Locale.US, "%02d:%02d", h, mi))
    }, h0, m0, true).show()
}

@Composable
private fun InfoCard(title: String, lines: List<String>, bg: Color) {
    Card(colors = CardDefaults.cardColors(containerColor = bg, contentColor = if (bg == MaterialTheme.colorScheme.errorContainer) MaterialTheme.colorScheme.onErrorContainer else Color(0xFF1F2933)), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            lines.forEach { Text("· $it", style = MaterialTheme.typography.bodySmall) }
        }
    }
}

// ─────────────────────────────────────────────────────────────
// 그래프
// ─────────────────────────────────────────────────────────────
private fun breakAnywhere(text: String): String = text.toList().joinToString("\u200B")

private fun copyUri(context: Context, uri: Uri, dst: File) {
    val input = context.contentResolver.openInputStream(uri)
        ?: throw IllegalArgumentException("선택한 파일을 열 수 없습니다.")
    input.use { i -> dst.outputStream().use { o -> i.copyTo(o) } }
}

private fun copyAsset(context: Context, name: String, dst: File) {
    context.assets.open(name).use { i -> dst.outputStream().use { o -> i.copyTo(o) } }
}
