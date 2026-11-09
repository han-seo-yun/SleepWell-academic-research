package app.sleepwell.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.sleepwell.analysis.NextNightModel
import app.sleepwell.analysis.ProfileStore
import java.io.File

@Composable
fun MyInformationScreen() {
    val context = LocalContext.current
    val versionName = remember(context) {
        context.packageManager.getPackageInfo(context.packageName, 0).versionName.orEmpty()
    }
    val file = remember { File(context.filesDir, ProfileStore.FILE_NAME) }
    var profile by remember { mutableStateOf(ProfileStore.load(file) ?: NextNightModel.profile) }
    var editing by rememberSaveable { mutableStateOf(false) }
    if (editing) {
        ProfileEditor(profile, firstRun = false, onCancel = { editing = false }, onSave = { updated ->
            try {
                ProfileStore.save(file, updated)
                NextNightModel.profile = updated
                profile = updated
                editing = false
                null
            } catch (e: Exception) { "저장하지 못했어요. ${e.message.orEmpty()}" }
        })
        return
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("내 정보", style = MaterialTheme.typography.headlineMedium)
        Text("나의 생활과 수면 목표", style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("희망 수면시간", style = MaterialTheme.typography.labelLarge)
                Text("${profile.desiredSleepMinutes / 60}시간 ${profile.desiredSleepMinutes % 60}분",
                    style = MaterialTheme.typography.headlineMedium)
                Text("평소 취침 ${profile.usualBed} · 기상 ${profile.usualWake}")
                HorizontalDivider()
                Text(ProfileStore.SEXES.firstOrNull { it.first == profile.sex }?.second.orEmpty())
                Button(onClick = { editing = true }, modifier = Modifier.fillMaxWidth()) { Text("기본정보·수면 목표 수정") }
            }
        }
        Text("이 정보는 기기에 저장됩니다. 희망 수면시간과 평소 시각은 개인화 초기값으로 사용하고, 성별·활동량은 프로필 정보로 보존합니다.",
            style = MaterialTheme.typography.bodyMedium)
        HorizontalDivider()
        Text("기기 내 HGB 하이브리드 예측", style = MaterialTheme.typography.titleMedium)
        Text(
            "수면 기록 특징 33개와 수리 특성 6개를 HGB 하이브리드로 계산하고, 기존 개인화와 기상 목표를 반영합니다. PC 서버 연결은 필요하지 않습니다.",
            style = MaterialTheme.typography.bodyMedium
        )
        Text("예측 모델: HGB 하이브리드 · 기기 내 오프라인 계산. 계산 중에도 기록을 확인할 수 있습니다.", style = MaterialTheme.typography.bodySmall)
        HorizontalDivider()
        Text("시간대 자동 · ${UiZone.current.zoneId}", style = MaterialTheme.typography.bodyMedium)
        Text("시각은 현재 기기의 시간대에 맞춰 표시합니다.", style = MaterialTheme.typography.bodySmall)
        Text("SleepWell $versionName", style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
