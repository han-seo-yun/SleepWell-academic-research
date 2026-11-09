package app.sleepwell.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.Slider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.style.TextAlign
import kotlin.math.roundToInt
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.NextNightModel
import app.sleepwell.analysis.ProfileStore

/** Shared first-run and edit form; a failed save leaves the user's draft visible. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ProfileEditor(
    initialProfile: NextNightModel.ModelProfile?,
    firstRun: Boolean,
    onSave: (NextNightModel.ModelProfile) -> String?,
    onCancel: (() -> Unit)? = null
) {
    val base = initialProfile ?: NextNightModel.ModelProfile()
    var sex by rememberSaveable { mutableStateOf(initialProfile?.sex.orEmpty()) }
    var activity by rememberSaveable { mutableStateOf(base.activityMinutes) }
    var desiredHours by rememberSaveable { mutableStateOf((base.desiredSleepMinutes / 60).toString()) }
    var desiredMinutes by rememberSaveable { mutableStateOf((base.desiredSleepMinutes % 60).toString()) }
    var bed by rememberSaveable { mutableStateOf(base.usualBed) }
    var wake by rememberSaveable { mutableStateOf(base.usualWake) }
    var error by rememberSaveable { mutableStateOf<String?>(null) }
    var onboardingStep by rememberSaveable { mutableStateOf(0) }
    val hasBasics = ProfileStore.SEXES.any { it.first == sex }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(if (firstRun) "맞춤 수면 분석 시작하기" else "내 정보 수정",
            style = MaterialTheme.typography.headlineSmall,
            fontWeight = if (firstRun) FontWeight.Bold else null)
        Text(if (firstRun) "기본 프로필과 수면 습관을 저장하면,\n삼성 헬스 기록과 연동된 모델로 분석을 시작합니다." else
            "저장하면 변경한 정보로 수면 분석을 다시 계산합니다.", style = MaterialTheme.typography.bodyMedium)

        if (firstRun) {
            Text("${onboardingStep + 1}/2 · ${if (onboardingStep == 0) "기본 프로필" else "수면 습관"}",
                style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
            LinearProgressIndicator(progress = { (onboardingStep + 1) / 2f }, modifier = Modifier.fillMaxWidth())
        }

        if (!firstRun || onboardingStep == 0) Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("기본 프로필", style = MaterialTheme.typography.titleMedium)
                Text("성별", style = MaterialTheme.typography.labelLarge)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    ProfileStore.SEXES.forEach { (value, label) ->
                        FilterChip(selected = sex == value, onClick = { sex = value; error = null }, label = { Text(label) })
                    }
                }
            }
        }

        if (!firstRun || onboardingStep == 1) Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("수면과 생활 습관", style = MaterialTheme.typography.titleMedium)
                Text("희망 수면시간", style = MaterialTheme.typography.labelLarge)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(value = desiredHours,
                        onValueChange = { desiredHours = it; error = null },
                        label = { Text("희망 수면 · 시간") }, singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.weight(1f))
                    OutlinedTextField(value = desiredMinutes,
                        onValueChange = { desiredMinutes = it; error = null },
                        label = { Text("희망 수면 · 분") }, singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.weight(1f))
                }
                Text("7~9시간 범위에서 분 단위로 직접 입력해 주세요. 예: 7시간 45분",
                    style = MaterialTheme.typography.bodySmall)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(value = bed, onValueChange = { bed = it; error = null },
                        label = { Text("평소 취침") }, placeholder = { Text("23:30") },
                        singleLine = true, modifier = Modifier.weight(1f))
                    OutlinedTextField(value = wake, onValueChange = { wake = it; error = null },
                        label = { Text("평소 기상") }, placeholder = { Text("07:30") },
                        singleLine = true, modifier = Modifier.weight(1f))
                }
                Text("24시간 형식으로 입력해 주세요. 예: 23:30, 07:30", style = MaterialTheme.typography.bodySmall)
                Text("하루 활동량", style = MaterialTheme.typography.labelLarge)
                Text("걷기·운동 등 몸을 움직이는 시간", style = MaterialTheme.typography.bodySmall)
                val activityIndex = ProfileStore.ACTIVITY.indexOfFirst {
                    it.second == ProfileStore.activityLabel(activity)
                }.coerceAtLeast(0)
                Row(verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(if (activityIndex < 2) "🚶" else "🏃",
                        style = MaterialTheme.typography.headlineMedium,
                        modifier = Modifier.semantics {
                            contentDescription = if (activityIndex < 2) "걷는 사람" else "달리는 사람"
                        })
                    Slider(
                        value = activityIndex.toFloat(),
                        onValueChange = { position ->
                            activity = ProfileStore.ACTIVITY[position.roundToInt().coerceIn(0, 2)].first
                        },
                        valueRange = 0f..2f,
                        steps = 1,
                        modifier = Modifier.weight(1f).semantics {
                            contentDescription = "하루 활동량"
                            stateDescription = ProfileStore.activityLabel(activity)
                        }
                    )
                }
                Row(Modifier.fillMaxWidth()) {
                    listOf("30분 미만", "30~90분", "90분 이상").forEachIndexed { index, label ->
                        Text(label, modifier = Modifier.weight(1f),
                            style = MaterialTheme.typography.labelSmall,
                            textAlign = when (index) {
                                0 -> TextAlign.Start
                                1 -> TextAlign.Center
                                else -> TextAlign.End
                            },
                            fontWeight = if (index == activityIndex) FontWeight.Bold else FontWeight.Normal,
                            color = if (index == activityIndex) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
        if (!firstRun || onboardingStep == 1) {
            Text("희망 수면시간과 평소 시각은 개인화 초기값으로 사용합니다. 활동량은 생활 정보로 저장됩니다.",
                style = MaterialTheme.typography.bodySmall)
        }
        if ((!firstRun || onboardingStep == 0) && !hasBasics)
            Text("성별을 선택해 주세요.", style = MaterialTheme.typography.bodySmall)
        error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium) }
        if (firstRun && onboardingStep == 0) Button(onClick = {
            error = null
            onboardingStep = 1
        }, enabled = hasBasics, modifier = Modifier.fillMaxWidth()) {
            Text("다음 · 수면 습관 입력")
        } else Button(onClick = {
            val desired = ProfileStore.parseDesiredSleepMinutes(desiredHours, desiredMinutes)
            if (desired == null) {
                error = "희망 수면시간은 7~9시간으로 입력해 주세요. 분은 0~59입니다."
            } else if (ProfileStore.validClock(bed.trim()) == null || ProfileStore.validClock(wake.trim()) == null) {
                error = "평소 시각은 23:30처럼 입력해 주세요."
            } else {
                error = onSave(base.copy(sex = sex,
                    activityMinutes = activity, desiredSleepMinutes = desired, usualBed = bed.trim(),
                    usualWake = wake.trim(), onboardingCompleted = true))
            }
        }, enabled = hasBasics, modifier = Modifier.fillMaxWidth()) {
            Text(if (firstRun) "저장하고 시작하기" else "저장하고 다시 계산")
        }
        if (firstRun && onboardingStep == 1) {
            TextButton(onClick = { error = null; onboardingStep = 0 }, modifier = Modifier.fillMaxWidth()) {
                Text("이전 · 기본 프로필")
            }
        }
        onCancel?.let { cancel ->
            TextButton(onClick = cancel, modifier = Modifier.align(Alignment.CenterHorizontally)) { Text("취소") }
        }
        Spacer(Modifier.height(24.dp))
    }
}
