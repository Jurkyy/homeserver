package dev.woto.homeserver.ui.remote

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.VolumeOff
import androidx.compose.material.icons.automirrored.rounded.VolumeUp
import androidx.compose.material.icons.rounded.Bedtime
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.Forward10
import androidx.compose.material.icons.rounded.Forward30
import androidx.compose.material.icons.rounded.LightMode
import androidx.compose.material.icons.rounded.MusicNote
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.PowerSettingsNew
import androidx.compose.material.icons.rounded.Replay10
import androidx.compose.material.icons.rounded.Replay30
import androidx.compose.material.icons.rounded.Settings
import androidx.compose.material.icons.rounded.Stop
import androidx.compose.material.icons.rounded.Videocam
import androidx.compose.material.icons.rounded.Wallpaper
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.blur
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.net.toUri
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil3.compose.AsyncImage
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.data.ConnectionStatus
import dev.woto.homeserver.data.ServerState
import dev.woto.homeserver.data.SpotifyState
import dev.woto.homeserver.data.StateSnapshot
import dev.woto.homeserver.data.sourceLabel
import dev.woto.homeserver.ui.components.LoadingState
import dev.woto.homeserver.ui.components.MessageState
import dev.woto.homeserver.ui.components.formatDuration
import dev.woto.homeserver.ui.components.rememberHaptic
import dev.woto.homeserver.ui.components.rememberImageUrl
import kotlinx.coroutines.delay

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RemoteScreen(graph: AppGraph, onOpenSettings: () -> Unit) {
    val snap by graph.repository.snapshot.collectAsStateWithLifecycle()
    val status by graph.repository.status.collectAsStateWithLifecycle()
    val state = snap?.state
    val config = LocalConfiguration.current
    val twoPane = config.screenWidthDp >= 600 && config.screenWidthDp > config.screenHeightDp

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        Backdrop(state?.takeIf { it.hasMedia }?.artworkUrl ?: state?.spotify?.takeIf { it.active }?.artworkUrl)
        Column(Modifier.fillMaxSize()) {
            TopAppBar(
                title = { Text("Homeserver") },
                actions = {
                    ConnectionPill(status)
                    IconButton(onClick = onOpenSettings) { Icon(Icons.Rounded.Settings, "Settings") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Color.Transparent),
                windowInsets = WindowInsets.statusBars,
            )
            when {
                state == null && (status == ConnectionStatus.Offline || status == ConnectionStatus.Unauthorized) -> MessageState(
                    title = if (status == ConnectionStatus.Unauthorized) "Token rejected" else "Can't reach the projector",
                    body = if (status == ConnectionStatus.Unauthorized) "The server refused the token. Re-pair from Settings." else "Make sure you're on the home Wi-Fi. Retrying in the background…",
                    actionLabel = if (status == ConnectionStatus.Unauthorized) "Open settings" else null,
                    onAction = onOpenSettings,
                )
                state == null -> LoadingState()
                twoPane -> Row(Modifier.fillMaxSize().padding(horizontal = 24.dp), horizontalArrangement = Arrangement.spacedBy(24.dp)) {
                    Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 24.dp)) {
                        NowPlaying(graph, snap!!)
                    }
                    Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                        Extras(graph, state)
                    }
                }
                else -> Column(
                    Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp).padding(bottom = 24.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp),
                ) {
                    NowPlaying(graph, snap!!)
                    Extras(graph, state)
                }
            }
        }
    }
}

@Composable
private fun Extras(graph: AppGraph, state: ServerState) {
    state.spotify?.takeIf { it.active }?.let { SpotifyCard(it) }
    ScreensaverSection(graph, state)
    PowerSection(graph, state)
}

@Composable
private fun Backdrop(artwork: String?) {
    val url = rememberImageUrl(artwork)
    val alpha by animateFloatAsState(if (url != null) 1f else 0f, label = "backdrop")
    if (url == null && alpha == 0f) return
    Box(Modifier.fillMaxWidth().height(420.dp)) {
        AsyncImage(
            model = url,
            contentDescription = null,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize().alpha(alpha * if (Build.VERSION.SDK_INT >= 31) 0.55f else 0.25f).blur(48.dp),
        )
        Box(
            Modifier.fillMaxSize().background(
                Brush.verticalGradient(listOf(Color.Transparent, MaterialTheme.colorScheme.background)),
            ),
        )
    }
}

@Composable
private fun ConnectionPill(status: ConnectionStatus) {
    val (label, color) = when (status) {
        ConnectionStatus.Connected -> "Live" to Color(0xFF6FDC8C)
        ConnectionStatus.Connecting, ConnectionStatus.Idle -> "Connecting" to MaterialTheme.colorScheme.secondary
        ConnectionStatus.Reconnecting -> "Reconnecting" to Color(0xFFFFC857)
        ConnectionStatus.Offline -> "Offline" to MaterialTheme.colorScheme.error
        ConnectionStatus.Unauthorized -> "Unauthorized" to MaterialTheme.colorScheme.error
    }
    val animated by animateColorAsState(color, label = "pill")
    Surface(shape = CircleShape, color = MaterialTheme.colorScheme.surfaceContainerHigh.copy(alpha = 0.7f)) {
        Row(Modifier.padding(horizontal = 10.dp, vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(animated))
            Spacer(Modifier.width(6.dp))
            Text(label, style = MaterialTheme.typography.labelMedium)
        }
    }
}

@Composable
private fun NowPlaying(graph: AppGraph, snap: StateSnapshot) {
    val s = snap.state
    AnimatedContent(
        targetState = when {
            s.isSleeping -> 0
            s.hasMedia -> 1
            else -> 2
        },
        transitionSpec = { fadeIn() togetherWith fadeOut() },
        label = "nowplaying",
    ) { mode ->
        when (mode) {
            0 -> SleepingHero(graph)
            1 -> PlayerCard(graph, snap)
            else -> IdleHero(s)
        }
    }
}

@Composable
private fun SleepingHero(graph: AppGraph) {
    val haptic = rememberHaptic()
    Card(
        Modifier.fillMaxWidth().padding(top = 8.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
        shape = RoundedCornerShape(28.dp),
    ) {
        Column(Modifier.fillMaxWidth().padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(Icons.Rounded.Bedtime, null, Modifier.size(56.dp), tint = MaterialTheme.colorScheme.primary)
            Text("Projector is sleeping", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(top = 12.dp))
            Text(
                "Casting anything wakes it up automatically.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp),
            )
            Button(onClick = { haptic(true); graph.actions.power("wake") }, modifier = Modifier.padding(top = 20.dp).height(52.dp)) {
                Icon(Icons.Rounded.LightMode, null)
                Spacer(Modifier.width(8.dp))
                Text("Wake projector")
            }
        }
    }
}

@Composable
private fun IdleHero(s: ServerState) {
    Card(
        Modifier.fillMaxWidth().padding(top = 8.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer.copy(alpha = 0.8f)),
        shape = RoundedCornerShape(28.dp),
    ) {
        Column(Modifier.fillMaxWidth().padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(
                if (s.screensaver?.active == true) Icons.Rounded.Wallpaper else Icons.Rounded.Videocam, null,
                Modifier.size(56.dp), tint = MaterialTheme.colorScheme.primary,
            )
            val themeName = s.screensaver?.let { ss -> ss.themes.firstOrNull { it.id == ss.theme }?.label ?: ss.theme }
            Text(
                if (s.screensaver?.active == true) "Screensaver: ${themeName ?: "on"}" else "Nothing playing",
                style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(top = 12.dp),
            )
            Text(
                "Pick a video from YouTube or Movies, or share a link to Homeserver from any app.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
    }
}

@Composable
private fun PlayerCard(graph: AppGraph, snap: StateSnapshot) {
    val s = snap.state
    val haptic = rememberHaptic()
    val art = rememberImageUrl(s.artworkUrl)

    // Interpolate the position locally between server events.
    val position by produceState(snap.positionAt(), snap) {
        while (true) {
            value = snap.positionAt(SystemClock.elapsedRealtime())
            if (!snap.state.isPlaying) break
            delay(250)
        }
    }
    var dragging by remember { mutableStateOf<Float?>(null) }

    Column(Modifier.fillMaxWidth().padding(top = 8.dp)) {
        Box(
            Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(24.dp))
                .background(MaterialTheme.colorScheme.surfaceContainerHigh),
            contentAlignment = Alignment.Center,
        ) {
            if (art != null) {
                if (s.source == "jellyfin") {
                    AsyncImage(art, null, Modifier.fillMaxSize().blur(24.dp).alpha(0.6f), contentScale = ContentScale.Crop)
                    AsyncImage(art, s.title, Modifier.fillMaxSize().padding(10.dp), contentScale = ContentScale.Fit)
                } else {
                    AsyncImage(art, s.title, Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
                }
            } else {
                Icon(Icons.Rounded.Videocam, null, Modifier.size(64.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }

        Row(Modifier.padding(top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            SourceChip(s.source)
            if (s.activity == "paused") {
                Spacer(Modifier.width(8.dp))
                Text("Paused", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Text(
            s.title ?: "Playing on projector",
            style = MaterialTheme.typography.headlineSmall, maxLines = 2, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 8.dp),
        )
        s.subtitle?.let {
            Text(it, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }

        // Seek bar
        val dur = s.durationS?.takeIf { it > 0 }
        if (dur != null) {
            val frac = dragging ?: ((position ?: 0.0) / dur).toFloat().coerceIn(0f, 1f)
            Slider(
                value = frac,
                onValueChange = { dragging = it },
                onValueChangeFinished = {
                    dragging?.let { graph.actions.seekTo(it * dur) }
                    dragging = null
                    haptic(false)
                },
                modifier = Modifier.padding(top = 8.dp),
            )
            Row(Modifier.fillMaxWidth()) {
                Text(formatDuration(dragging?.let { it * dur } ?: position), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.weight(1f))
                Text(formatDuration(dur), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        } else {
            LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 20.dp, bottom = 8.dp))
        }

        // Transport
        Row(
            Modifier.fillMaxWidth().padding(top = 8.dp),
            horizontalArrangement = Arrangement.SpaceEvenly,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TransportButton(Icons.Rounded.Replay30, "Back 30 seconds") { haptic(false); graph.actions.seekBy(-30.0) }
            TransportButton(Icons.Rounded.Replay10, "Back 10 seconds") { haptic(false); graph.actions.seekBy(-10.0) }
            FilledIconButton(
                onClick = { haptic(true); graph.actions.togglePlay() },
                modifier = Modifier.size(80.dp),
                shape = RoundedCornerShape(if (s.isPlaying) 24.dp else 40.dp),
            ) {
                Icon(if (s.isPlaying) Icons.Rounded.Pause else Icons.Rounded.PlayArrow, if (s.isPlaying) "Pause" else "Play", Modifier.size(44.dp))
            }
            TransportButton(Icons.Rounded.Forward10, "Forward 10 seconds") { haptic(false); graph.actions.seekBy(10.0) }
            TransportButton(Icons.Rounded.Forward30, "Forward 30 seconds") { haptic(false); graph.actions.seekBy(30.0) }
        }

        // Volume + stop
        VolumeRow(graph, s, Modifier.padding(top = 12.dp))
        OutlinedButton(
            onClick = { haptic(true); graph.actions.stop() },
            modifier = Modifier.align(Alignment.CenterHorizontally).padding(top = 4.dp),
        ) {
            Icon(Icons.Rounded.Stop, null)
            Spacer(Modifier.width(6.dp))
            Text("Stop")
        }
    }
}

@Composable
private fun TransportButton(icon: androidx.compose.ui.graphics.vector.ImageVector, label: String, onClick: () -> Unit) {
    FilledTonalIconButton(onClick = onClick, modifier = Modifier.size(52.dp)) {
        Icon(icon, label, Modifier.size(28.dp))
    }
}

@Composable
private fun VolumeRow(graph: AppGraph, s: ServerState, modifier: Modifier = Modifier) {
    var dragging by remember { mutableStateOf<Float?>(null) }
    val value = dragging ?: s.volume.toFloat()
    Row(modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Icon(
            if (s.muted || value <= 0f) Icons.AutoMirrored.Rounded.VolumeOff else Icons.AutoMirrored.Rounded.VolumeUp,
            "Volume", tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Slider(
            value = value,
            onValueChange = { dragging = it },
            onValueChangeFinished = {
                dragging?.let { graph.actions.setVolume(it.toDouble()) }
                dragging = null
            },
            valueRange = 0f..100f,
            modifier = Modifier.weight(1f).padding(horizontal = 12.dp),
            colors = SliderDefaults.colors(thumbColor = MaterialTheme.colorScheme.secondary, activeTrackColor = MaterialTheme.colorScheme.secondary),
        )
        Text("${value.toInt()}", style = MaterialTheme.typography.labelLarge, modifier = Modifier.width(32.dp))
    }
}

@Composable
private fun SourceChip(source: String?) {
    Surface(shape = CircleShape, color = MaterialTheme.colorScheme.primaryContainer) {
        Text(
            sourceLabel(source),
            style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onPrimaryContainer,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
        )
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 8.dp, bottom = 4.dp))
}

@Composable
private fun SpotifyCard(sp: SpotifyState) {
    val context = LocalContext.current
    val art = rememberImageUrl(sp.artworkUrl)
    val spotifyGreen = Color(0xFF1DB954)
    Card(
        Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(24.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
    ) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(64.dp).clip(RoundedCornerShape(12.dp)).background(MaterialTheme.colorScheme.surfaceContainerHigh), contentAlignment = Alignment.Center) {
                if (art != null) AsyncImage(art, sp.album, Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
                else Icon(Icons.Rounded.MusicNote, null, tint = spotifyGreen)
            }
            Column(Modifier.weight(1f).padding(horizontal = 14.dp)) {
                Text(
                    if (sp.playing) "Spotify · playing on Homeserver" else "Spotify · paused",
                    style = MaterialTheme.typography.labelMedium, color = spotifyGreen,
                )
                Text(sp.title ?: "Connected", style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    listOfNotNull(sp.artist, sp.album).joinToString(" · ").ifEmpty { "Control playback from the Spotify app" },
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
                val dur = sp.durationS?.takeIf { it > 0 }
                if (dur != null) {
                    val pos by produceState(sp.positionAt(), sp) {
                        while (true) {
                            value = sp.positionAt()
                            if (!sp.playing) break
                            delay(1000)
                        }
                    }
                    LinearProgressIndicator(
                        progress = { ((pos ?: 0.0) / dur).toFloat().coerceIn(0f, 1f) },
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                        color = spotifyGreen,
                        drawStopIndicator = {},
                    )
                }
            }
            FilledTonalButton(
                onClick = { openSpotify(context) },
                colors = ButtonDefaults.filledTonalButtonColors(containerColor = spotifyGreen.copy(alpha = 0.2f), contentColor = spotifyGreen),
            ) { Text("Open") }
        }
    }
}

private fun openSpotify(context: Context) {
    val launch = context.packageManager.getLaunchIntentForPackage("com.spotify.music")
    val intent = launch ?: Intent(Intent.ACTION_VIEW, "https://open.spotify.com".toUri())
    try {
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    } catch (_: ActivityNotFoundException) {
    }
}

@Composable
private fun ScreensaverSection(graph: AppGraph, s: ServerState) {
    val ss = s.screensaver ?: return
    if (ss.themes.isEmpty() && !ss.active) return
    val haptic = rememberHaptic()
    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            SectionTitle("Screensaver")
            Spacer(Modifier.weight(1f))
            ss.idleMinutes?.let {
                Text("auto after ${it.toInt()} min idle", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            AnimatedVisibility(ss.active) {
                AssistChip(
                    onClick = { haptic(false); graph.actions.screensaver(null) },
                    label = { Text("Stop") },
                    leadingIcon = { Icon(Icons.Rounded.Close, null, Modifier.size(AssistChipDefaults.IconSize)) },
                )
            }
            ss.themes.forEach { theme ->
                FilterChip(
                    selected = ss.active && ss.theme == theme.id,
                    onClick = { haptic(false); graph.actions.screensaver(theme.id) },
                    label = { Text(theme.label) },
                )
            }
        }
    }
}

@Composable
private fun PowerSection(graph: AppGraph, s: ServerState) {
    var confirmPowerOff by remember { mutableStateOf(false) }
    val haptic = rememberHaptic()
    Column {
        SectionTitle("Power")
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            if (s.isSleeping) {
                FilledTonalButton(onClick = { haptic(true); graph.actions.power("wake") }, modifier = Modifier.weight(1f)) {
                    Icon(Icons.Rounded.LightMode, null); Spacer(Modifier.width(8.dp)); Text("Wake")
                }
            } else {
                FilledTonalButton(onClick = { haptic(true); graph.actions.power("sleep") }, modifier = Modifier.weight(1f)) {
                    Icon(Icons.Rounded.Bedtime, null); Spacer(Modifier.width(8.dp)); Text("Sleep")
                }
            }
            OutlinedButton(
                onClick = { confirmPowerOff = true },
                modifier = Modifier.weight(1f),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error),
            ) {
                Icon(Icons.Rounded.PowerSettingsNew, null); Spacer(Modifier.width(8.dp)); Text("Power off")
            }
        }
    }
    if (confirmPowerOff) {
        AlertDialog(
            onDismissRequest = { confirmPowerOff = false },
            icon = { Icon(Icons.Rounded.PowerSettingsNew, null) },
            title = { Text("Shut down the server?") },
            text = { Text("The media server powers off completely. You'll need to switch it on by hand. Use Sleep to just blank the projector.") },
            confirmButton = {
                TextButton(onClick = { confirmPowerOff = false; haptic(true); graph.actions.power("poweroff") }) {
                    Text("Power off", color = MaterialTheme.colorScheme.error, fontWeight = FontWeight.SemiBold)
                }
            },
            dismissButton = { TextButton(onClick = { confirmPowerOff = false }) { Text("Cancel") } },
        )
    }
}

