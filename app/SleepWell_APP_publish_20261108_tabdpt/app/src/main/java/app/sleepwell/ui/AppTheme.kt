package app.sleepwell.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.material3.Typography
import androidx.compose.material3.Shapes
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** v8 design tokens; see DESIGN.md Visual language. */
private val Navy = Color(0xFF404C8A)
private val NavySoft = Color(0xFFE5E8FF)

private val LightColors = lightColorScheme(
    primary = Navy,
    onPrimary = Color.White,
    primaryContainer = NavySoft,
    onPrimaryContainer = Navy,
    secondary = Color(0xFF006B62),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFC7F1E8),          // 선택된 칩 바탕
    onSecondaryContainer = Color(0xFF003D37),
    background = Color(0xFFF5F6FC),
    onBackground = Color(0xFF1F2933),
    surface = Color.White,
    onSurface = Color(0xFF1F2933),
    surfaceVariant = Color(0xFFF4F5F7),
    onSurfaceVariant = Color(0xFF4A5363),
    surfaceContainerLowest = Color.White,
    surfaceContainerLow = Color.White,
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
    secondary = Color(0xFF82D5C5),
    onSecondary = Color(0xFF00382F),
    secondaryContainer = Color(0xFF145047),
    onSecondaryContainer = Color(0xFFBFF1E4),
    background = Color(0xFF101423),
    onBackground = Color(0xFFE2E7EE),
    surface = Color(0xFF191E30),
    onSurface = Color(0xFFE2E7EE),
    surfaceVariant = Color(0xFF252A31),
    onSurfaceVariant = Color(0xFFC1C8D2),
    surfaceContainerLowest = Color(0xFF101318),
    surfaceContainerLow = Color(0xFF191E30),
    surfaceContainer = Color(0xFF1D2127),
    surfaceContainerHigh = Color(0xFF242930),
    surfaceContainerHighest = Color(0xFF2A3038),
    outline = Color(0xFF87909C),
    outlineVariant = Color(0xFF414851),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005)
)

object SleepWellSpacing {
    val small = 8.dp
    val medium = 12.dp
    val content = 16.dp
    val section = 24.dp
}

private val SleepTypography = Typography(
    headlineMedium = TextStyle(fontSize = 28.sp, lineHeight = 36.sp, fontWeight = FontWeight.Bold),
    titleLarge = TextStyle(fontSize = 22.sp, lineHeight = 30.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(fontSize = 18.sp, lineHeight = 26.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontSize = 14.sp, lineHeight = 21.sp),
    labelLarge = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    labelMedium = TextStyle(fontSize = 13.sp, lineHeight = 19.sp, fontWeight = FontWeight.Medium),
    labelSmall = TextStyle(fontSize = 12.sp, lineHeight = 18.sp)
)
private val SleepShapes = Shapes(small = RoundedCornerShape(12.dp), medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(20.dp), extraLarge = RoundedCornerShape(28.dp))

@Composable
fun SleepWellTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors, typography = SleepTypography, shapes = SleepShapes, content = content)
}
