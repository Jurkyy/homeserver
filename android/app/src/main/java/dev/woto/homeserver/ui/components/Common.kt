package dev.woto.homeserver.ui.components

import android.view.HapticFeedbackConstants
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CloudOff
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.woto.homeserver.data.Urls
import dev.woto.homeserver.graph
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import kotlin.math.roundToLong

@Composable
fun LoadingState(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
}

@Composable
fun MessageState(
    title: String,
    modifier: Modifier = Modifier,
    body: String? = null,
    icon: ImageVector = Icons.Rounded.CloudOff,
    actionLabel: String? = null,
    onAction: (() -> Unit)? = null,
) {
    Column(
        modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(icon, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 16.dp), textAlign = TextAlign.Center)
        if (body != null) {
            Text(
                body, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
        }
        if (actionLabel != null && onAction != null) {
            FilledTonalButton(onClick = onAction, modifier = Modifier.padding(top = 20.dp)) { Text(actionLabel) }
        }
    }
}

/** Haptic tick for transport buttons. */
@Composable
fun rememberHaptic(): (confirm: Boolean) -> Unit {
    val view = LocalView.current
    return remember(view) {
        { confirm: Boolean ->
            view.performHapticFeedback(
                if (confirm && android.os.Build.VERSION.SDK_INT >= 30) HapticFeedbackConstants.CONFIRM else HapticFeedbackConstants.KEYBOARD_TAP,
            )
        }
    }
}

/** Resolves a server-relative image URL against the paired base URL. */
@Composable
fun rememberImageUrl(path: String?): String? {
    val base = LocalContext.current.graph.settings.current.baseUrl
    return remember(base, path) { Urls.resolve(base, path) }
}

fun formatDuration(seconds: Double?): String {
    if (seconds == null || seconds.isNaN() || seconds < 0) return "--:--"
    val total = seconds.roundToLong()
    val h = total / 3600
    val m = (total % 3600) / 60
    val s = total % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

fun formatRuntime(seconds: Double?): String? {
    if (seconds == null || seconds <= 0) return null
    val mins = (seconds / 60).roundToLong()
    return if (mins >= 60) "${mins / 60}h ${mins % 60}m" else "${mins}m"
}

fun relativeTime(iso: String?, now: Instant = Instant.now()): String? {
    if (iso.isNullOrBlank()) return null
    val t = runCatching { OffsetDateTime.parse(iso).toInstant() }.recoverCatching { Instant.parse(iso) }.getOrNull() ?: return null
    val d = Duration.between(t, now)
    val mins = d.toMinutes()
    return when {
        mins < 1 -> "just now"
        mins < 60 -> "${mins}m ago"
        mins < 60 * 24 -> "${mins / 60}h ago"
        mins < 60 * 24 * 7 -> "${mins / (60 * 24)}d ago"
        mins < 60 * 24 * 30 -> "${mins / (60 * 24 * 7)}w ago"
        mins < 60 * 24 * 365 -> "${mins / (60 * 24 * 30)}mo ago"
        else -> "${mins / (60 * 24 * 365)}y ago"
    }
}
