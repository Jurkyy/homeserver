package dev.woto.homeserver.ui.settings

import android.os.Build
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.automirrored.rounded.VolumeUp
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.Info
import androidx.compose.material.icons.rounded.LinkOff
import androidx.compose.material.icons.rounded.Notifications
import androidx.compose.material.icons.rounded.Palette
import androidx.compose.material.icons.rounded.QrCodeScanner
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.BuildConfig
import dev.woto.homeserver.data.ConnectionStatus
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(graph: AppGraph, onBack: () -> Unit, onRepair: () -> Unit) {
    val settings by graph.settings.settings.collectAsStateWithLifecycle()
    val status by graph.repository.status.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var confirmUnpair by remember { mutableStateOf(false) }
    val serverVersion by produceState<String?>(null, settings.baseUrl) {
        value = settings.baseUrl?.let { runCatching { graph.api.health(it).version }.getOrNull() }
    }

    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Settings") },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Back") } },
            windowInsets = WindowInsets.statusBars,
        )
        Column(Modifier.verticalScroll(rememberScrollState()).windowInsetsPadding(WindowInsets.navigationBars)) {
            Header("Server")
            ListItem(
                headlineContent = { Text(settings.baseUrl ?: "Not paired") },
                supportingContent = {
                    Text(
                        listOfNotNull(
                            when (status) {
                                ConnectionStatus.Connected -> "Connected"
                                ConnectionStatus.Unauthorized -> "Token rejected"
                                ConnectionStatus.Offline -> "Offline"
                                else -> "Connecting…"
                            },
                            serverVersion?.let { "version $it" },
                        ).joinToString(" · "),
                    )
                },
                leadingContent = { Icon(Icons.Rounded.Dns, null) },
            )
            ListItem(
                headlineContent = { Text("Re-pair / change server") },
                supportingContent = { Text("Scan a new QR code or enter URL and token") },
                leadingContent = { Icon(Icons.Rounded.QrCodeScanner, null) },
                modifier = Modifier.clickable(onClick = onRepair),
            )
            ListItem(
                headlineContent = { Text("Forget token", color = MaterialTheme.colorScheme.error) },
                leadingContent = { Icon(Icons.Rounded.LinkOff, null, tint = MaterialTheme.colorScheme.error) },
                modifier = Modifier.clickable { confirmUnpair = true },
            )
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            Header("Controls")
            Toggle(
                Icons.Rounded.Notifications, "Media notification",
                "Lock-screen and notification controls while something plays on the projector",
                settings.mediaNotification,
            ) { scope.launch { graph.settings.setMediaNotification(it) } }
            Toggle(
                Icons.AutoMirrored.Rounded.VolumeUp, "Volume keys control projector",
                "While the app is open, the phone's volume buttons change the projector volume",
                settings.volumeKeys,
            ) { scope.launch { graph.settings.setVolumeKeys(it) } }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                Header("Appearance")
                Toggle(Icons.Rounded.Palette, "Dynamic color", "Use colors from your wallpaper", settings.dynamicColor) {
                    scope.launch { graph.settings.setDynamicColor(it) }
                }
            }
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            ListItem(
                headlineContent = { Text("Homeserver for Android") },
                supportingContent = { Text("Version ${BuildConfig.VERSION_NAME} · API v1") },
                leadingContent = { Icon(Icons.Rounded.Info, null) },
            )
        }
    }

    if (confirmUnpair) {
        AlertDialog(
            onDismissRequest = { confirmUnpair = false },
            title = { Text("Forget this server?") },
            text = { Text("The token is removed from this phone. You'll need to pair again.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmUnpair = false
                    scope.launch { graph.settings.unpair(); onRepair() }
                }) { Text("Forget") }
            },
            dismissButton = { TextButton(onClick = { confirmUnpair = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun Header(text: String) {
    Text(text, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(start = 16.dp, top = 12.dp, bottom = 4.dp))
}

@Composable
private fun Toggle(icon: ImageVector, title: String, body: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    ListItem(
        headlineContent = { Text(title) },
        supportingContent = { Text(body) },
        leadingContent = { Icon(icon, null) },
        trailingContent = { Switch(checked = checked, onCheckedChange = onChange) },
        modifier = Modifier.clickable { onChange(!checked) },
    )
}
