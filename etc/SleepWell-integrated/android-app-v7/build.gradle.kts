// 루트 빌드 파일. 플러그인을 여기서 "선언만" 하고 적용은 app 모듈에서 한다.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
