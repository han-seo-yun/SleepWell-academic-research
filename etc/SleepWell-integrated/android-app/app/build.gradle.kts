plugins {
    alias(libs.plugins.android.application)
    // Kotlin 은 AGP 9 에 내장돼 있으므로 별도 플러그인을 적용하지 않는다.
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "app.sleepwell"
    compileSdk = 37

    defaultConfig {
        applicationId = "app.sleepwell"
        // Samsung Health Data SDK 실기기 조건과 일치시킨다.
        minSdk = 29
        targetSdk = 37
        versionCode = 7
        versionName = "0.4.5"
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
    debugImplementation(libs.androidx.ui.tooling)
}
