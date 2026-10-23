package dev.woto.homeserver.ui.theme

import android.os.Build
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// "Projector at night" palette: deep ink background, warm amber primary (lamp light), cool cyan accent.
private val Amber = Color(0xFFFFB86B)
private val OnAmber = Color(0xFF3B2200)
private val AmberContainer = Color(0xFF5A3A12)
private val Cyan = Color(0xFF7FD3E8)
private val Ink = Color(0xFF0E1015)
private val InkRaised = Color(0xFF171A21)
private val InkHigh = Color(0xFF20242D)

private val BrandDark = darkColorScheme(
    primary = Amber,
    onPrimary = OnAmber,
    primaryContainer = AmberContainer,
    onPrimaryContainer = Color(0xFFFFDDB8),
    secondary = Cyan,
    onSecondary = Color(0xFF00363F),
    secondaryContainer = Color(0xFF1F3A42),
    onSecondaryContainer = Color(0xFFB8EAF6),
    tertiary = Color(0xFFC6B4FF),
    background = Ink,
    onBackground = Color(0xFFE6E2DA),
    surface = Ink,
    onSurface = Color(0xFFE6E2DA),
    surfaceVariant = InkHigh,
    onSurfaceVariant = Color(0xFFB9B4AA),
    surfaceContainerLowest = Color(0xFF0A0B0F),
    surfaceContainerLow = Color(0xFF13151B),
    surfaceContainer = InkRaised,
    surfaceContainerHigh = InkHigh,
    surfaceContainerHighest = Color(0xFF2A2F39),
    outline = Color(0xFF5B5F69),
    outlineVariant = Color(0xFF353943),
    error = Color(0xFFFFB4AB),
)

private val AppTypography = Typography().let { t ->
    t.copy(
        displaySmall = t.displaySmall.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.5).sp),
        headlineSmall = t.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
        titleLarge = t.titleLarge.copy(fontWeight = FontWeight.SemiBold),
        labelLarge = t.labelLarge.copy(fontWeight = FontWeight.SemiBold),
    )
}

@Composable
fun HomeserverTheme(dynamicColor: Boolean = true, content: @Composable () -> Unit) {
    val colors = if (dynamicColor && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        dynamicDarkColorScheme(LocalContext.current).copy(background = Ink, surface = Ink)
    } else {
        BrandDark
    }
    MaterialTheme(colorScheme = colors, typography = AppTypography, content = content)
}
