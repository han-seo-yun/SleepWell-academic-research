package app.sleepwell.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/*
 * 헬스 커넥트 "개인정보 처리방침" 화면 (소빈 · 2026-10-27)
 *  - 헬스 커넥트 권한 창의 "개인정보 처리방침" 링크를 누르면 열리는 화면.
 *  - 이 화면(매니페스트의 activity + activity-alias)이 없으면 안드로이드가 권한 창을 아예 띄우지 않는다.
 *  - 연구용 시제품이라 실제 처리방침 대신 무엇을 읽고 어디에 쓰는지만 적는다.
 */
class HealthPermissionsRationaleActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            SleepWellTheme {
                Surface(Modifier.fillMaxSize()) {
                    Column(Modifier.padding(24.dp)) {
                        Text("SleepWell 건강 데이터 사용 안내", style = MaterialTheme.typography.titleLarge)
                        Spacer(Modifier.height(12.dp))
                        Text(
                            "· 읽는 데이터: 헬스 커넥트의 수면 기록(잠든·깬 시각, 수면 단계)만 읽어요. 쓰거나 지우지 않아요.\n" +
                                "· 쓰는 곳: 이 폰 안에서 수면 압력·일주기 리듬을 계산하고 수면 일정을 보여주는 데만 써요.\n" +
                                "· 서버로 보내지 않아요. 분석 결과와 설문은 이 앱 안에만 저장돼요.\n" +
                                "· 백그라운드 읽기를 허용하면 앱을 닫아도 Android가 허용하는 시점에 수면 기록을 가져와 기기에 누적해요. 다음 실행 때 DATA33과 개인화 예측에 반영해요.\n" +
                                "· 언제든 설정 → 헬스 커넥트 → 앱 권한에서 허용을 끌 수 있어요.\n\n" +
                                "학술제 SleepWell 팀의 연구용 시제품이에요. 의학적 진단이 아니에요.",
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Spacer(Modifier.height(20.dp))
                        Button(onClick = { finish() }) { Text("닫기") }
                    }
                }
            }
        }
    }
}
