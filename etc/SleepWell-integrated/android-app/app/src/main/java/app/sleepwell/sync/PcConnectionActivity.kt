package app.sleepwell.sync

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
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class PcConnectionActivity : ComponentActivity() {
    private var permissionResult: ((Boolean) -> Unit)? = null
    private val requestHealthPermissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        permissionResult?.invoke(it[HealthPermissions.READ_SLEEP] == true)
        permissionResult = null
    }
    private var localNetworkPermissionResult: ((Boolean) -> Unit)? = null
    private val requestLocalNetworkPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        localNetworkPermissionResult?.invoke(it)
        localNetworkPermissionResult = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    PcConnectionScreen(
                        requestPermission = { callback ->
                            if (Build.VERSION.SDK_INT < 34) callback(false)
                            else {
                                permissionResult = callback
                                requestHealthPermissions.launch(arrayOf(
                                    HealthPermissions.READ_SLEEP,
                                    HealthPermissions.READ_STEPS,
                                    HealthPermissions.READ_EXERCISE
                                ))
                            }
                        },
                        requestLocalNetworkPermission = { callback ->
                            if (Build.VERSION.SDK_INT < 37) callback(true)
                            else {
                                localNetworkPermissionResult = callback
                                requestLocalNetworkPermission.launch(LOCAL_NETWORK_PERMISSION)
                            }
                        },
                        openPermissionSettings = {
                            if (Build.VERSION.SDK_INT >= 34) {
                                startActivity(
                                    Intent(HealthConnectManager.ACTION_MANAGE_HEALTH_PERMISSIONS)
                                        .putExtra(Intent.EXTRA_PACKAGE_NAME, packageName)
                                )
                            }
                        },
                        openPrivacy = { startActivity(Intent(this, PrivacyPolicyActivity::class.java)) }
                    )
                }
            }
        }
    }
}

@Composable
private fun PcConnectionScreen(
    requestPermission: (((Boolean) -> Unit) -> Unit),
    requestLocalNetworkPermission: (((Boolean) -> Unit) -> Unit),
    openPermissionSettings: () -> Unit,
    openPrivacy: () -> Unit
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val repository = remember { SyncRepository(context.applicationContext) }
    val reader = remember { HealthConnectSleepReader(context.applicationContext) }
    val scope = rememberCoroutineScope()
    var pairUri by remember { mutableStateOf("") }
    var connected by remember { mutableStateOf(repository.isConnected()) }
    var hasPending by remember { mutableStateOf(repository.hasPending()) }
    var endpoint by remember { mutableStateOf(repository.connectionLabel()) }
    var hasPermission by remember { mutableStateOf(reader.hasPermission()) }
    var hasLocalNetworkPermission by remember { mutableStateOf(hasLocalNetworkPermission(context)) }
    var busy by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    fun launchWork(block: suspend () -> String) {
        busy = true
        error = null
        status = null
        scope.launch {
            runCatching { withContext(Dispatchers.IO) { block() } }
                .onSuccess { status = it }
                .onFailure { error = it.message ?: "처리 중 오류가 발생했습니다." }
            connected = repository.isConnected()
            hasPending = repository.hasPending()
            endpoint = repository.connectionLabel()
            hasPermission = reader.hasPermission()
            hasLocalNetworkPermission = hasLocalNetworkPermission(context)
            busy = false
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("SleepWell PC 연결", style = MaterialTheme.typography.headlineSmall)
            Text("Samsung Health가 Health Connect에 저장한 최근 30일 수면·걸음·운동과 직접 입력한 일정·낮잠·카페인을 같은 Wi-Fi의 내 PC로 보냅니다.")

            if (Build.VERSION.SDK_INT < 34) {
                Notice("이 기능은 Android 14 이상에서 사용할 수 있습니다.", true)
            } else {
                Notice(
                    if (hasPermission) "Health Connect 수면 읽기 권한: 허용됨"
                    else "Health Connect 수면 읽기 권한이 필요합니다.",
                    !hasPermission
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { requestPermission { hasPermission = it } }, enabled = !busy) {
                        Text("건강 데이터 권한 허용")
                    }
                    OutlinedButton(onClick = openPermissionSettings, enabled = !busy) { Text("권한 철회·관리") }
                }
            }

            if (Build.VERSION.SDK_INT >= 37) {
                Notice(
                    if (hasLocalNetworkPermission) "로컬 네트워크 권한: 허용됨"
                    else "Windows PC와 통신하려면 로컬 네트워크 권한이 필요합니다.",
                    !hasLocalNetworkPermission
                )
                Button(
                    onClick = { requestLocalNetworkPermission { hasLocalNetworkPermission = it } },
                    enabled = !busy
                ) { Text("로컬 네트워크 권한 허용") }
            }

            if (connected) {
                Notice("연결된 PC: ${endpoint ?: "확인 불가"}", false)
                Button(
                    onClick = { launchWork { repository.sync().message } },
                    enabled = !busy && hasLocalNetworkPermission,
                    modifier = Modifier.fillMaxWidth()
                ) { Text(if (busy) "동기화 중…" else "지금 동기화") }
                Text(
                    "PC의 모바일 연결 수신 기능을 먼저 켜야 합니다. PC가 꺼져 있거나 연결되지 않으면 기록을 지우지 않고 다음 동기화 때 다시 시도합니다.",
                    style = MaterialTheme.typography.bodySmall
                )
                if (hasPending) {
                    OutlinedButton(
                        onClick = {
                            repository.discardPending()
                            hasPending = false
                            status = "전송 대기 자료를 폐기했습니다. 마지막 성공 기록은 보존되어 다음 동기화에서 안전하게 다시 비교합니다."
                        },
                        enabled = !busy,
                        modifier = Modifier.fillMaxWidth()
                    ) { Text("대기 자료 폐기 후 다시 조회") }
                }
                OutlinedButton(
                    onClick = {
                        repository.disconnect()
                        connected = false
                        endpoint = null
                        hasPending = false
                        status = "PC 연결 정보와 동기화 기록을 이 휴대폰에서 삭제했습니다."
                    },
                    enabled = !busy,
                    modifier = Modifier.fillMaxWidth()
                ) { Text("PC 연결 해제") }
            } else {
                OutlinedTextField(
                    value = pairUri,
                    onValueChange = { pairUri = it.trim() },
                    label = { Text("Windows 앱에 표시된 연결 주소 붙여넣기") },
                    placeholder = { Text("sleepwell://pair?…") },
                    minLines = 3,
                    modifier = Modifier.fillMaxWidth()
                )
                Button(
                    onClick = {
                        val value = pairUri
                        launchWork { "${repository.pair(value)}에 안전하게 연결했습니다." }
                    },
                    enabled = !busy && pairUri.isNotBlank() && hasLocalNetworkPermission,
                    modifier = Modifier.fillMaxWidth()
                ) { Text(if (busy) "연결 중…" else "PC와 연결") }
            }

            status?.let { Notice(it, false) }
            error?.let { Notice(it, true) }

            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("개인정보 보호", style = MaterialTheme.typography.titleMedium)
                    Text("건강 데이터는 사용자가 ‘지금 동기화’를 누를 때만 읽습니다. Samsung Health 원본 중 수면·걸음·운동만 전송하며, 권한 일부를 거절해도 허용된 자료와 직접 입력한 생활 정보는 동기화합니다.")
                    Text("기록 값이나 연결 토큰을 로그에 남기지 않습니다. 연결 토큰과 재시도 자료는 Android Keystore로 암호화하며 앱 백업에서 제외됩니다.")
                    OutlinedButton(onClick = openPrivacy) { Text("자세한 개인정보 안내") }
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

private const val LOCAL_NETWORK_PERMISSION = "android.permission.ACCESS_LOCAL_NETWORK"

private fun hasLocalNetworkPermission(context: android.content.Context): Boolean =
    Build.VERSION.SDK_INT < 37 ||
        context.checkSelfPermission(LOCAL_NETWORK_PERMISSION) == android.content.pm.PackageManager.PERMISSION_GRANTED

@Composable
private fun Notice(text: String, isError: Boolean) {
    Card(Modifier.fillMaxWidth()) {
        Text(
            text,
            color = if (isError) MaterialTheme.colorScheme.error else Color(0xFF176B35),
            modifier = Modifier.fillMaxWidth().padding(12.dp)
        )
    }
}
