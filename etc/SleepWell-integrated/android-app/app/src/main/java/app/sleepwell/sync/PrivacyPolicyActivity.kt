package app.sleepwell.sync

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

class PrivacyPolicyActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    Column(
                        Modifier.padding(18.dp).verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text("SleepWell 개인정보 안내", style = MaterialTheme.typography.headlineSmall)
                        Text("사용 목적")
                        Text("다음날 수면 예측과 개인화 결과를 검증하기 위해 Samsung Health가 Health Connect에 제공한 수면·걸음·운동 기록과 사용자가 직접 입력한 일정·낮잠·카페인 정보를 사용합니다.")
                        Text("읽는 정보")
                        Text("최근 30일의 완료된 수면 세션·수면 단계, 걸음 구간·걸음 수, 운동 구간, 원본 식별자와 수정 시각만 읽습니다. 출처가 com.sec.android.app.shealth인 기록만 선택하며 캘린더나 센서에서 카페인·일정을 추측하지 않습니다.")
                        Text("전송과 보관")
                        Text("사용자가 직접 동기화할 때 같은 사설망의 연결된 Windows PC로 TLS 전송합니다. Android 17 이상에서는 로컬 네트워크 권한을 별도로 요청합니다. 서버 인증서 SHA-256 지문을 확인하며, 연결 토큰과 실패 후 재시도 자료는 Android Keystore로 암호화합니다.")
                        Text("삭제와 통제")
                        Text("‘PC 연결 해제’를 누르면 휴대폰의 연결 토큰과 동기화 대기 자료를 삭제합니다. Health Connect 권한은 ‘권한 철회·관리’에서 언제든 철회할 수 있습니다. 권한이 없거나 조회가 끝까지 완료되지 않으면 삭제 내역을 만들지 않습니다.")
                        Text("자동 백그라운드 수집")
                        Text("현재 버전은 백그라운드에서 상시 수집하지 않습니다. 화면에서 ‘지금 동기화’를 누른 경우에만 동작합니다.")
                    }
                }
            }
        }
    }
}
