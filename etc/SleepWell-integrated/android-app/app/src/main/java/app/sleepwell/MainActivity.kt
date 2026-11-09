package app.sleepwell

import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
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
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import app.sleepwell.export.ExportResult
import app.sleepwell.export.Exporters
import app.sleepwell.health.EpochAggregator
import app.sleepwell.health.EpochRow
import app.sleepwell.health.FakeDataSource
import app.sleepwell.health.HealthDataSource
import app.sleepwell.health.HealthQuery
import app.sleepwell.health.HealthSnapshot
import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.HealthResult
import app.sleepwell.health.RidgeResidualModel
import app.sleepwell.health.SleepStage
import app.sleepwell.health.SnapshotAnalysisPipeline
import app.sleepwell.health.TimeFmt
import app.sleepwell.health.readSnapshot
import app.sleepwell.ui.SleepStageChart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import kotlin.math.roundToInt

/*
 * MVP 화면 하나.
 *
 * 흐름:  피험자 ID · 기간 선택  →  [조회]  →  요약 + 그래프 확인  →  [내보내기]  →  파일 공유
 *
 * 설계 메모
 *  - ViewModel 을 쓰지 않고 remember 로 상태를 들고 있다. lifecycle-viewmodel-compose 는
 *    Android Studio 기본 템플릿 의존성에 없어서, 쓰려면 Gradle 을 건드려야 한다.
 *    화면이 하나뿐이라 이걸로 충분하다.
 *  - 테마도 템플릿이 만드는 이름(예: MyApplicationTheme)에 의존하지 않도록
 *    MaterialTheme 을 직접 쓴다. 프로젝트 이름이 뭐가 되든 그대로 붙는다.
 *  - 데이터 소스는 HealthDataSource 인터페이스로만 받는다. 채윤의 SamsungHealthSource 가
 *    준비되면 아래 dataSourceOf() 한 곳만 고치면 실제 데이터로 돌아간다.
 */

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) { CollectorScreen() }
            }
        }
    }
}

/**
 * 쓸 데이터 소스를 고른다. **통합 지점은 여기 한 곳이다.**
 *
 * 채윤의 구현이 들어오면:
 *   SamsungHealthSource(context)  를 돌려주고, 실패 시 FakeDataSource() 로 떨어지게 한다.
 */
private fun dataSourceOf(@Suppress("UNUSED_PARAMETER") context: Context): HealthDataSource = FakeDataSource()

/** 파일 크기 표시. 1KB 미만은 바이트로 그대로 보여준다. */
private fun fileSizeText(bytes: Long): String =
    if (bytes < 1024) "${bytes}B" else "${bytes / 1024}KB"

private const val DAY_MS = 24 * 60 * 60 * 1000L

@Composable
fun CollectorScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var subjectId by remember { mutableStateOf("sobin01") }
    var days by remember { mutableStateOf(7) }
    var endShiftDays by remember { mutableStateOf(0) }      // 0 = 오늘까지
    var watchOnly by remember { mutableStateOf(true) }

    var loading by remember { mutableStateOf(false) }
    var snapshot by remember { mutableStateOf<HealthSnapshot?>(null) }
    var epochs by remember { mutableStateOf<List<EpochRow>>(emptyList()) }
    var problems by remember { mutableStateOf<List<String>>(emptyList()) }
    var exported by remember { mutableStateOf<ExportResult?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var analysis by remember { mutableStateOf<AnalysisResult?>(null) }

    val endMs = System.currentTimeMillis() - endShiftDays * DAY_MS
    val startMs = endMs - days * DAY_MS

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("Sleepwell 연구용 가상 데이터 데모", style = MaterialTheme.typography.headlineSmall)
        Text(
            "웨어러블 건강 데이터를 조회해 30초 epoch CSV 로 내보냅니다.",
            style = MaterialTheme.typography.bodySmall
        )

        Spacer(Modifier.height(16.dp))

        // ── 조회 조건 ──────────────────────────────────────────
        OutlinedTextField(
            value = subjectId,
            onValueChange = { subjectId = it.trim() },
            label = { Text("피험자 ID (파일명·subject_id 에 들어갑니다)") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )

        Spacer(Modifier.height(12.dp))
        Text("조회 기간", style = MaterialTheme.typography.labelLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf(1, 3, 7, 14).forEach { d ->
                FilterChip(
                    selected = days == d,
                    onClick = { days = d },
                    label = { Text("${d}일") }
                )
            }
        }

        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedButton(onClick = { endShiftDays += 1 }) { Text("◀ 이전") }
            Spacer(Modifier.height(0.dp))
            Text(
                "  ${TimeFmt.day(startMs)} ~ ${TimeFmt.day(endMs)}  ",
                style = MaterialTheme.typography.bodyMedium
            )
            OutlinedButton(
                onClick = { if (endShiftDays > 0) endShiftDays -= 1 },
                enabled = endShiftDays > 0
            ) { Text("다음 ▶") }
        }

        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(checked = watchOnly, onCheckedChange = { watchOnly = it })
            Text("  시계 데이터만 (폰 자체 기록·수동 입력 제외)")
        }
        Text(
            "⚠ 이 화면은 항상 가상 데이터만 만듭니다. 내보낸 파일에는 source=\"fake\" 와 파일명 _FAKE 가 붙습니다. 실제 측정값이나 연구용 학습에 쓰지 마세요.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(0xFFB25E00)
        )

        Spacer(Modifier.height(16.dp))

        // ── 조회 ──────────────────────────────────────────────
        Button(
            onClick = {
                loading = true
                message = null
                exported = null
                analysis = null
                scope.launch {
                    val src = dataSourceOf(context)
                    val result = withContext(Dispatchers.Default) {
                        val q = HealthQuery(startMs, endMs, watchOnly)
                        if (!src.isAvailable()) null
                        else {
                            if (!src.hasPermissions()) src.requestPermissions()
                            src.readSnapshot(subjectId.ifBlank { "unknown" }, q)
                        }
                    }
                    if (result == null) {
                        message = "데이터 소스를 사용할 수 없습니다. (${src.name})"
                        snapshot = null
                        epochs = emptyList()
                    } else {
                        snapshot = result
                        epochs = EpochAggregator.aggregate(result)
                        problems = EpochAggregator.selfCheck(epochs)
                        val ridge = runCatching {
                            context.assets.open("ridge_residual.json").bufferedReader().use { reader ->
                                RidgeResidualModel.fromJson(reader.readText())
                            }
                        }.getOrNull()
                        when (val modelResult = SnapshotAnalysisPipeline.analyze(result, residualModel = ridge)) {
                            is HealthResult.Success -> analysis = modelResult.data
                            is HealthResult.Unavailable -> message = "추천 계산: ${modelResult.reason}"
                            is HealthResult.PermissionDenied -> message = "추천 계산 권한이 없습니다."
                            is HealthResult.Failure -> message = "추천 계산 실패: ${modelResult.error.message}"
                        }
                    }
                    loading = false
                }
            },
            enabled = !loading,
            modifier = Modifier.fillMaxWidth()
        ) { Text(if (loading) "조회 중..." else "조회") }

        if (loading) {
            Spacer(Modifier.height(12.dp))
            CircularProgressIndicator()
        }

        message?.let {
            Spacer(Modifier.height(12.dp))
            Text(it, color = MaterialTheme.colorScheme.error)
        }

        // ── 결과 ──────────────────────────────────────────────
        snapshot?.let { snap ->
            Spacer(Modifier.height(20.dp))
            HorizontalDivider()
            Spacer(Modifier.height(16.dp))

            SummaryCard(snap, epochs)

            analysis?.let { result ->
                Spacer(Modifier.height(12.dp))
                RecommendationCard(result)
            }

            if (snap.warnings.isNotEmpty()) {
                Spacer(Modifier.height(12.dp))
                WarnCard("조회 경고", snap.warnings, Color(0xFFFFF4E5))
            }
            if (problems.isNotEmpty()) {
                Spacer(Modifier.height(12.dp))
                WarnCard("규격 점검 실패 — 내보내기 전에 확인 필요", problems, Color(0xFFFDE7E9))
            }

            Spacer(Modifier.height(20.dp))
            Text("수면 그래프", style = MaterialTheme.typography.titleMedium)
            SleepStageChart(epochs, Modifier.fillMaxWidth())

            Spacer(Modifier.height(20.dp))
            Button(
                onClick = {
                    scope.launch {
                        val r = withContext(Dispatchers.IO) { Exporters.exportAll(context, snap) }
                        exported = r
                        message = null
                    }
                },
                enabled = epochs.isNotEmpty(),
                modifier = Modifier.fillMaxWidth()
            ) { Text("CSV · JSON 내보내기") }

            exported?.let { ex ->
                Spacer(Modifier.height(12.dp))
                Card(colors = CardDefaults.cardColors(containerColor = Color(0xFFE9F5EC))) {
                    Column(Modifier.padding(12.dp)) {
                        Text("내보내기 완료", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(4.dp))
                        Text(
                            ex.folder.absolutePath,
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace
                        )
                        Spacer(Modifier.height(6.dp))
                        ex.files.forEach {
                            Text(
                                // 1KB 미만을 KB 로 나누면 "0KB" 가 되어 파일이 빈 것처럼 보인다.
                                // meta.json 이 실제로 700바이트대라 이 문제가 눈에 띄었다.
                                "· ${it.name}  (${fileSizeText(it.length())})",
                                style = MaterialTheme.typography.bodySmall
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(onClick = { shareFiles(context, ex.files) }) {
                            Text("파일 공유 (메일·드라이브로 보내기)")
                        }
                    }
                }
            }
        }

        Spacer(Modifier.height(40.dp))
    }
}

@Composable
private fun RecommendationCard(result: AnalysisResult) {
    val rec = result.recommendation ?: return
    Card(colors = CardDefaults.cardColors(containerColor = Color(0xFFE8F1FF)), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("오늘의 수면 추천", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            InfoRow("권장 취침", rec.recommendedBedTimeMs?.let(TimeFmt::hhmm) ?: "계산 불가")
            InfoRow("권장 기상", rec.recommendedWakeTimeMs?.let(TimeFmt::hhmm) ?: "계산 불가")
            InfoRow("권장 수면", rec.recommendedSleepMin?.roundToInt()?.let { "${it}분" } ?: "-")
            InfoRow("현재 수면압력 S", String.format("%.3f", result.currentS))
            Text(
                if (rec.usedFallback) "설문 학습 전: 현재는 물리모델 기준 추천입니다."
                else "물리모델에 ${rec.residualModelVersion} 개인화 잔차를 적용했습니다.",
                style = MaterialTheme.typography.bodySmall,
                color = if (rec.usedFallback) Color(0xFF8A5A00) else Color(0xFF176B35)
            )
        }
    }
}

@Composable
private fun SummaryCard(snap: HealthSnapshot, epochs: List<EpochRow>) {
    val sum = EpochAggregator.summarize(epochs)
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("요약", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            InfoRow("데이터 소스", if (snap.sourceTag == "samsung") "Samsung Health" else "가상 데이터 (fake)")
            InfoRow("조회 구간", "${TimeFmt.day(snap.queriedFrom)} ~ ${TimeFmt.day(snap.queriedTo)}")
            // 낮잠이 있으면 세션 수 > 밤 수. 둘을 따로 보여줘야 "왜 3일 조회인데 세션이 4개지?"
            // 하고 데이터가 잘못된 것으로 오해하지 않는다.
            InfoRow("수면 세션", if (sum.sessions > sum.nights) "${sum.sessions}개 (${sum.nights}일 밤 · 낮잠 포함)"
                                 else "${sum.sessions}개 (${sum.nights}일 밤)")
            InfoRow("총 epoch", "${sum.totalEpochs}칸 (30초 × ${sum.totalEpochs})")
            InfoRow("수면 시간 합계", TimeFmt.durationText(sum.sleepDurationMs))
            InfoRow("심박 표본", "${snap.heartRate.size}개")
            InfoRow("심박이 채워진 epoch", "${(sum.hrCoverage * 100).toInt()}%")
            InfoRow("걸음 구간", "${snap.steps.size}개")
            if (sum.unknownEpochs > 0) {
                InfoRow("단계 미상 epoch", "${sum.unknownEpochs}칸 (stage_label = -1, 평가에서 제외)")
            }

            Spacer(Modifier.height(10.dp))
            Text("단계 분포", style = MaterialTheme.typography.labelLarge)
            val total = epochs.count { it.stageLabel in 0..4 }.coerceAtLeast(1)
            listOf(SleepStage.WAKE, SleepStage.REM, SleepStage.N1, SleepStage.N2, SleepStage.N3)
                .forEach { st ->
                    val n = sum.stageCounts[st.code] ?: 0
                    val pct = n * 100.0 / total
                    InfoRow(
                        st.label,
                        String.format("%,d칸 · %.1f%% · %s", n, pct, TimeFmt.durationText(n * 30_000L))
                    )
                }
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(label, style = MaterialTheme.typography.bodySmall)
        Text(value, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun WarnCard(title: String, lines: List<String>, bg: Color) {
    Card(colors = CardDefaults.cardColors(containerColor = bg), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            lines.forEach { Text("· $it", style = MaterialTheme.typography.bodySmall) }
        }
    }
}

/**
 * 내보낸 파일을 다른 앱(Gmail·Drive 등)으로 넘긴다.
 * FileProvider 가 필요하다 — AndroidManifest 설정은 README_소빈.md 3단계 참고.
 * 설정이 안 돼 있으면 예외가 나므로 앱이 죽지 않도록 감싸 둔다.
 */
private fun shareFiles(context: Context, files: List<File>) {
    try {
        val uris = files.map {
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", it)
        }
        val intent = Intent(Intent.ACTION_SEND_MULTIPLE).apply {
            type = "*/*"
            putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris))
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(intent, "내보낸 파일 공유"))
    } catch (e: Exception) {
        // 공유가 안 되더라도 파일은 이미 저장돼 있다. Android Studio 의
        // Device Explorer 로 직접 꺼낼 수 있으므로 치명적이지 않다.
        android.widget.Toast.makeText(
            context,
            "공유 실패: ${e.message}\n파일은 저장되어 있습니다. 위 경로를 확인하세요.",
            android.widget.Toast.LENGTH_LONG
        ).show()
    }
}
