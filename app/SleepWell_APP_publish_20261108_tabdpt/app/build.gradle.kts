plugins {
    alias(libs.plugins.android.application)
    // Kotlin 은 AGP 9 에 내장돼 있으므로 별도 플러그인을 적용하지 않는다.
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "app.sleepwell"
    compileSdk = 37

    defaultConfig {
        applicationId = "app.sleepwell.offlinehgb"
        // 서윤 언니 팀 repo(2026-10-07)와 맞춤: Samsung Health Data SDK 실기기 조건
        minSdk = 29
        targetSdk = 37
        versionCode = 16
        versionName = "0.8.8"
        testInstrumentationRunner = "android.test.InstrumentationTestRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    buildFeatures {
        compose = true
    }
    androidResources {
        noCompress += listOf("bin", "json")
    }
    useLibrary("android.test.runner")
    useLibrary("android.test.base")
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    // (2026-10-27) 삼성 헬스 → 헬스 커넥트 수면 읽기
    implementation(libs.androidx.health.connect)
    debugImplementation(libs.androidx.ui.tooling)
}
