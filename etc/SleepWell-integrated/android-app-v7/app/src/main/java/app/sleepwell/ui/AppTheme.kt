package app.sleepwell.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/*
 * 앱 색 (소빈 · 2026-10-24)
 *  - 기본 Material 보라 대신 흰 바탕 + 차분한 남색 하나로 정리 (2026-10-24 소빈 요청)
 *  - 카드 기본 바탕은 아주 연한 회색 → 흰 바탕 위에서 구분만 되게
 *  - 그래프 선 색(수면 압력 파랑·일주기 리듬 주황·성향 보라)은 의미 색이라 그대로 둔다 (AnalysisScreen)
 */
private val Navy = Color(0xFF2F4A6D)
private val NavySoft = Color(0xFFE6ECF4)

private val LightColors = lightColorScheme(
    primary = Navy,
    onPrimary = Color.White,
    primaryContainer = NavySoft,
    onPrimaryContainer = Navy,
    secondary = Navy,
    onSecondary = Color.White,
    secondaryContainer = NavySoft,          // 선택된 칩 바탕
    onSecondaryContainer = Navy,
    background = Color.White,
    onBackground = Color(0xFF1F2933),
    surface = Color.White,
    onSurface = Color(0xFF1F2933),
    surfaceVariant = Color(0xFFF4F5F7),
    onSurfaceVariant = Color(0xFF4A5363),
    surfaceContainerLowest = Color.White,
    surfaceContainerLow = Color(0xFFFAFAFB),
    surfaceContainer = Color(0xFFF6F7F9),
    surfaceContainerHigh = Color(0xFFF4F5F7),
    surfaceContainerHighest = Color(0xFFF4F5F7),   // Card 기본 바탕
    outline = Color(0xFFC9CED6),
    outlineVariant = Color(0xFFE2E5EA)
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFFB7CAE5),
    onPrimary = Color(0xFF17304F),
    primaryContainer = Color(0xFF304967),
    onPrimaryContainer = Color(0xFFDCE8F8),
    secondary = Color(0xFFB7CAE5),
    onSecondary = Color(0xFF17304F),
    secondaryContainer = Color(0xFF304967),
    onSecondaryContainer = Color(0xFFDCE8F8),
    background = Color(0xFF111418),
    onBackground = Color(0xFFE2E7EE),
    surface = Color(0xFF171A1F),
    onSurface = Color(0xFFE2E7EE),
    surfaceVariant = Color(0xFF252A31),
    onSurfaceVariant = Color(0xFFC1C8D2),
    surfaceContainerLowest = Color(0xFF101318),
    surfaceContainerLow = Color(0xFF171A1F),
    surfaceContainer = Color(0xFF1D2127),
    surfaceContainerHigh = Color(0xFF242930),
    surfaceContainerHighest = Color(0xFF2A3038),
    outline = Color(0xFF87909C),
    outlineVariant = Color(0xFF414851),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005)
)

@Composable
fun SleepWellTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors, content = content)
}
