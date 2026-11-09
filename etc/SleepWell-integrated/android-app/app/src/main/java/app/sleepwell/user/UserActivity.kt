package app.sleepwell.user

import android.content.Intent
import android.health.connect.HealthConnectManager
import android.health.connect.HealthPermissions
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import app.sleepwell.sync.PcConnectionActivity
import app.sleepwell.sync.PrivacyPolicyActivity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.LocalDate
import kotlin.math.roundToInt

class UserActivity : ComponentActivity() {
    private var permissionCallback: ((Boolean) -> Unit)? = null
    private val permissionRequest = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        permissionCallback?.invoke(it); permissionCallback = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    UserScreen(
                        requestSleepPermission = { callback ->
                            if (Build.VERSION.SDK_INT < 34) callback(false) else {
                                permissionCallback = callback; permissionRequest.launch(HealthPermissions.READ_SLEEP)
                            }
                        },
                        openHealthSettings = {
                            if (Build.VERSION.SDK_INT >= 34) startActivity(Intent(HealthConnectManager.ACTION_MANAGE_HEALTH_PERMISSIONS)
                                .putExtra(Intent.EXTRA_PACKAGE_NAME, packageName))
                        },
                        openPc = { startActivity(Intent(this, PcConnectionActivity::class.java)) },
                        openPrivacy = { startActivity(Intent(this, PrivacyPolicyActivity::class.java)) }
                    )
                }
            }
        }
    }
}

@Composable
private fun UserScreen(
    requestSleepPermission: (((Boolean) -> Unit) -> Unit),
    openHealthSettings: () -> Unit,
    openPc: () -> Unit,
    openPrivacy: () -> Unit
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val repository = remember { UserRepository(context.applicationContext) }
    val lifecycleOwner = context as? LifecycleOwner
    val backup = remember { BackupManager(context.applicationContext) }
    val scope = rememberCoroutineScope()
    var state by remember { mutableStateOf(repository.load()) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf(state.status.orEmpty()) }

    var age by remember { mutableStateOf(state.profile?.ageBand ?: "18-29") }
    var sex by remember { mutableStateOf(state.profile?.sex ?: "응답 안 함") }
    var occupation by remember { mutableStateOf(state.profile?.occupation ?: "") }
    var activity by remember { mutableStateOf((state.profile?.activityMinutes ?: 60).toString()) }
    var usualBed by remember { mutableStateOf(state.profile?.usualBed ?: "23:30") }
    var usualWake by remember { mutableStateOf(state.profile?.usualWake ?: "07:00") }
    var desired by remember { mutableStateOf((state.profile?.desiredSleepMinutes ?: 450).toString()) }
    var diaryDay by remember { mutableStateOf(LocalDate.now(UserRepository.ZONE).toString()) }
    var diaryBed by remember { mutableStateOf("23:30") }
    var diaryWake by remember { mutableStateOf("07:00") }
    var rested by remember { mutableStateOf("") }
    var fatigue by remember { mutableStateOf("") }

    fun work(block: suspend () -> String) {
        busy = true
        scope.launch {
            runCatching { withContext(Dispatchers.IO) { block() } }
                .onSuccess { message = it }.onFailure { message = it.message ?: "처리 중 오류가 발생했습니다." }
            state = repository.load(); busy = false
        }
    }

    LaunchedEffect(Unit) {
        if (state.profile != null) state = withContext(Dispatchers.IO) { repository.refresh() }
        BackupScheduler.schedule(context, state.autoBackup)
        val today = LocalDate.now(UserRepository.ZONE)
        val backedUpDay = state.lastLocalBackup?.let { runCatching { Instant.parse(it).atZone(UserRepository.ZONE).toLocalDate() }.getOrNull() }
        if (state.autoBackup && state.profile != null && backedUpDay != today) {
            withContext(Dispatchers.IO) { backup.createTodaySnapshot() }
            state = repository.load()
        }
    }
    LaunchedEffect(lifecycleOwner) {
        while (true) {
            delay(60_000)
            if (lifecycleOwner?.lifecycle?.currentState?.isAtLeast(Lifecycle.State.RESUMED) == true && state.profile != null) {
                state = withContext(Dispatchers.IO) { repository.runScheduledRecommendationSlot(awake = true) }
            }
        }
    }
    LaunchedEffect(state.recommendation?.generatedAt) {
        state.recommendation?.generatedAt?.let { generatedAt ->
            state = withContext(Dispatchers.IO) { repository.markRecommendationViewed(generatedAt) }
        }
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("SleepWell", style = MaterialTheme.typography.headlineMedium)
        Text("휴대폰 안에서 수면 기록과 개인 목표를 계산합니다. 모델 출력은 연구용 생활 패턴 참고이며 의료 진단이나 치료 권고가 아닙니다.")

        Section("내 기준") {
            Field(age, { age = it }, "연령대 (18-29/30-44/45-59/60-80)")
            Field(sex, { sex = it }, "성별 (여성/남성/응답 안 함)")
            Field(occupation, { occupation = it }, "직업·주요 역할")
            Field(activity, { activity = it }, "하루 활동시간(분)")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Field(usualBed, { usualBed = it }, "평소 취침 HH:mm", Modifier.weight(1f))
                Field(usualWake, { usualWake = it }, "평소 기상 HH:mm", Modifier.weight(1f))
            }
            Field(desired, { desired = it }, "희망 수면시간(분)")
            Button(onClick = { work {
                state = repository.saveProfile(age, sex, occupation, activity.toInt(), usualBed, usualWake, desired.toInt())
                "프로필을 저장했습니다. 추천은 개인화된 첫 시각 또는 다음 조건부 갱신 시각에 반영됩니다."
            } }, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("프로필 저장·추천 갱신") }
        }

        state.recommendation?.let { r ->
            Section("2. 권장 수면 기회") {
                if (r.planFeasible) {
                    Text("권장 취침 ${r.bedtime} · 기상 ${r.wakeTime} · ${r.durationMinutes}분")
                } else {
                    Text("현재 시각과 내일 일정 사이에 목표 수면시간을 확보하기 어렵습니다.")
                    if (r.durationMinutes > 0) Text("확보 가능한 구간 ${r.bedtime} ~ ${r.wakeTime} · ${r.durationMinutes}분")
                }
                Text(r.explanation)
                Text("이 값은 확보할 수면 기회이며 개인의 생리적 최적 수면량을 측정한 값은 아닙니다.")
                r.regularityMinutes?.let { Text("최근 취침 규칙성 편차 약 ${it}분") }
                r.subjectiveQuality?.let { Text("최근 주관적 회복감 ${it}/5") }
                r.processS?.let { Text("계산된 Process S ${"%.3f".format(it)} · 고정 시간상수를 사용한 모델값이며 생리 측정값이 아닙니다.") }
            }
        }
        DailyContextEditor(value = state.profile?.dailyContext, enabled = !busy && state.profile != null, onSave = { value ->
            work {
                repository.saveDailyContext(value)
                "다음날 일정·낮잠·카페인 정보를 저장했습니다. 다음 조건부 갱신 시각에 반영됩니다."
            }
        })
        state.forecast?.let { f ->
            Section("1. 다음 주수면 예측") {
                val predicted = f.ensembleBedOffset?.roundToInt()?.let(::offsetText) ?: "자료 부족"
                Text("연구 모델이 본 다음 주수면 예상 시각: $predicted")
                Text(f.note)
                Text("예측은 실제 행동 시각의 추정이며 권장 일정과 구분합니다.")
                state.recommendationTiming?.let { timing ->
                    Text("첫 추천 ${timing.initialDecisionAt} · 공식 연구 기준 ${timing.modelCutoffAt}")
                    Text("앱을 보고 있는 동안 20·21·22·23시에 입력이 바뀐 경우에만 조건부 갱신합니다.")
                }
            }
        }

        state.modelOutcomes?.let { outcomes ->
            val recent = outcomes.optJSONObject("primary_observed_outcomes")
            val baseline = outcomes.optJSONObject("baseline_observed_outcomes")
            Section("3. 기록된 변화") {
                if (outcomes.optBoolean("baseline_comparable") && recent != null && baseline != null) {
                    Text("이전 7회와 최근 14회의 단순 기술 비교입니다. 추천 효과의 인과 검증은 아닙니다.")
                    Text("취침 규칙성 편차: ${jsonMinutes(baseline, "bedtime_regularity_mad_min")} → ${jsonMinutes(recent, "bedtime_regularity_mad_min")}")
                    Text("회복감 평균: ${jsonScore(baseline, "self_reported_rested_mean")} → ${jsonScore(recent, "self_reported_rested_mean")}")
                    Text("피로도 평균: ${jsonScore(baseline, "self_reported_fatigue_mean")} → ${jsonScore(recent, "self_reported_fatigue_mean")}")
                } else {
                    Text("비교 가능한 자기보고 기록이 아직 부족합니다. 저장된 미래 예측과 첫 실제 결과는 덮어쓰지 않고 누적합니다.")
                }
                Text("동결 예측 ${state.prospectiveForecasts.size}건 · 첫 실제 결과 ${state.prospectiveOutcomes.size}건 · 화면 노출 ${state.recommendationExposures.size}건")
            }
        }

        Section("수면 일기") {
            Field(diaryDay, { diaryDay = it }, "기상일 YYYY-MM-DD")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Field(diaryBed, { diaryBed = it }, "잠든 시각 HH:mm (추정)", Modifier.weight(1f))
                Field(diaryWake, { diaryWake = it }, "기상 HH:mm", Modifier.weight(1f))
            }
            Text("잠자리에 누운 시각과 구분해 실제로 잠든 시각을 추정해 입력하세요.")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Field(rested, { rested = it }, "회복감 1~5 (선택)", Modifier.weight(1f))
                Field(fatigue, { fatigue = it }, "피로도 1~5 (선택)", Modifier.weight(1f))
            }
            Button(onClick = { work {
                val restedValue = if (rested.isBlank()) null else rested.toInt()
                val fatigueValue = if (fatigue.isBlank()) null else fatigue.toInt()
                state = repository.addDiary(diaryDay, diaryBed, diaryWake, restedValue, fatigueValue)
                "수면 일기를 저장하고 추천을 다시 계산했습니다."
            } }, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("일기 저장") }
        }

        Section("Samsung Health") {
            Text("가져오기는 화면에서 직접 누를 때만 실행하며, 백그라운드에서는 저장된 기록만 사용합니다.")
            Button(onClick = { requestSleepPermission { granted ->
                if (granted) work { val (_, count) = repository.importSamsung(); "Samsung Health에서 ${count}건을 가져왔습니다." }
                else message = "수면 읽기 권한이 없어 가져오지 않았습니다."
            } }, enabled = !busy && Build.VERSION.SDK_INT >= 34, modifier = Modifier.fillMaxWidth()) { Text("권한 허용·지금 가져오기") }
            OutlinedButton(onClick = openHealthSettings, modifier = Modifier.fillMaxWidth()) { Text("권한 관리") }
        }

        Section("연구용 PC 백업") {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("자동 로컬 백업·전송 재시도")
                Switch(checked = state.autoBackup, onCheckedChange = {
                    state = repository.setAutoBackup(it); BackupScheduler.schedule(context, it)
                })
            }
            Text("클라우드를 사용하지 않습니다. PC가 꺼져 있으면 암호화된 대기 자료를 휴대폰에 보존합니다.")
            Text("최근 로컬 백업: ${state.lastLocalBackup ?: "없음"}")
            Text("최근 PC 전달: ${state.lastPcDelivery ?: "없음"} · 대기 ${backup.pendingCount()}건")
            Button(onClick = { work {
                backup.createTodaySnapshot(); val sent = runCatching { backup.sendPending() }.getOrElse { BackupScheduler.scheduleRetry(context); throw it }
                "오늘 백업을 저장하고 PC에 ${sent}건 전달했습니다."
            } }, enabled = !busy && state.profile != null, modifier = Modifier.fillMaxWidth()) { Text("지금 백업/전송") }
            OutlinedButton(onClick = openPc, modifier = Modifier.fillMaxWidth()) { Text("PC 연결 설정") }
            OutlinedButton(onClick = openPrivacy, modifier = Modifier.fillMaxWidth()) { Text("개인정보·권한 안내") }
        }
        if (message.isNotBlank()) Card(Modifier.fillMaxWidth()) { Text(message, Modifier.padding(12.dp)) }
    }
}

@Composable private fun Section(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium); content()
    } }
}

@Composable private fun Field(value: String, change: (String) -> Unit, label: String, modifier: Modifier = Modifier.fillMaxWidth()) =
    OutlinedTextField(value = value, onValueChange = change, label = { Text(label) }, singleLine = true, modifier = modifier)

private fun offsetText(offset: Int): String {
    val minute = ((20 * 60 + offset) % (24 * 60) + 24 * 60) % (24 * 60)
    return "%02d:%02d".format(minute / 60, minute % 60)
}

private fun jsonMinutes(value: org.json.JSONObject, key: String): String =
    if (!value.has(key) || value.isNull(key)) "자료 부족" else "${value.getDouble(key).roundToInt()}분"

private fun jsonScore(value: org.json.JSONObject, key: String): String =
    if (!value.has(key) || value.isNull(key)) "자료 부족" else "${"%.1f".format(value.getDouble(key))}/5"
