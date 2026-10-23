package dev.woto.homeserver.ui.movies

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.Replay
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.layout.layout
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.data.JfItem
import dev.woto.homeserver.data.userMessage
import dev.woto.homeserver.ui.LocalSnackbar
import dev.woto.homeserver.ui.components.LoadingState
import dev.woto.homeserver.ui.components.MessageState
import dev.woto.homeserver.ui.components.formatDuration
import dev.woto.homeserver.ui.components.formatRuntime
import dev.woto.homeserver.ui.components.rememberHaptic
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class DetailUi(val item: JfItem? = null, val loading: Boolean = true, val error: String? = null)

class DetailViewModel(private val graph: AppGraph, private val id: String) : ViewModel() {
    private val _ui = MutableStateFlow(DetailUi())
    val ui: StateFlow<DetailUi> = _ui.asStateFlow()

    init { load() }

    fun load() {
        _ui.value = _ui.value.copy(loading = _ui.value.item == null, error = null)
        viewModelScope.launch {
            runCatching { graph.api.jfItem(id) }
                .onSuccess { _ui.value = DetailUi(it, loading = false) }
                .onFailure { _ui.value = _ui.value.copy(loading = false, error = it.userMessage()) }
        }
    }
}

@Composable
fun JfDetailScreen(graph: AppGraph, id: String, onBack: () -> Unit, onOpenItem: (JfItem) -> Unit) {
    val vm: DetailViewModel = viewModel(key = "jf-$id") { DetailViewModel(graph, id) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    val snackbar = LocalSnackbar.current
    val scope = rememberCoroutineScope()
    val haptic = rememberHaptic()
    var busy by remember { mutableStateOf(false) }
    val settings by graph.settings.settings.collectAsStateWithLifecycle()

    fun play(item: JfItem, resume: Boolean) {
        if (busy) return
        busy = true
        haptic(true)
        scope.launch {
            graph.actions.playJellyfin(item.id, resume, lowQuality = settings.jfLowQuality)
                .onSuccess { snackbar.showSnackbar("Playing “${item.name}” on the projector") }
                .onFailure { snackbar.showSnackbar("Couldn't play: ${it.userMessage()}") }
            busy = false
        }
    }

    Box(Modifier.fillMaxSize()) {
        val item = ui.item
        when {
            ui.loading -> LoadingState()
            item == null -> MessageState("Couldn't load", body = ui.error, actionLabel = "Retry", onAction = vm::load)
            else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 32.dp)) {
                item(key = "header") { Header(item) }
                item(key = "actions") {
                    Column(Modifier.padding(horizontal = 20.dp)) {
                        if (item.isPlayable) {
                            Row(Modifier.padding(top = 16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                if (item.canResume) {
                                    Button(onClick = { play(item, true) }, enabled = !busy, modifier = Modifier.height(48.dp)) {
                                        Icon(Icons.Rounded.PlayArrow, null)
                                        Spacer(Modifier.width(6.dp))
                                        Text("Resume ${formatDuration(item.resumeS)}")
                                    }
                                    FilledTonalButton(onClick = { play(item, false) }, enabled = !busy, modifier = Modifier.height(48.dp)) {
                                        Icon(Icons.Rounded.Replay, null)
                                        Spacer(Modifier.width(6.dp))
                                        Text("From start")
                                    }
                                } else {
                                    Button(onClick = { play(item, false) }, enabled = !busy, modifier = Modifier.height(48.dp)) {
                                        Icon(Icons.Rounded.PlayArrow, null)
                                        Spacer(Modifier.width(6.dp))
                                        Text("Play on projector")
                                    }
                                }
                            }
                        }
                        if (item.isPlayable || item.type == "Season") {
                            FilterChip(
                                selected = settings.jfLowQuality,
                                onClick = { scope.launch { graph.settings.setJfLowQuality(!settings.jfLowQuality) } },
                                label = { Text("Low bandwidth (~1.2 Mbps)") },
                                modifier = Modifier.padding(top = 8.dp),
                            )
                        }
                        item.overview?.takeIf { it.isNotBlank() }?.let {
                            Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 16.dp))
                        }
                    }
                }
                val children = item.children.orEmpty()
                if (children.isNotEmpty()) {
                    item(key = "children-title") {
                        Text(
                            when (item.type) { "Series" -> "Seasons"; "Season" -> "Episodes"; else -> "Items" },
                            style = MaterialTheme.typography.titleMedium,
                            modifier = Modifier.padding(start = 20.dp, top = 24.dp, bottom = 8.dp),
                        )
                    }
                    items(children, key = { "c-${it.id}" }) { child ->
                        if (child.isPlayable) EpisodeRow(child, onOpen = { onOpenItem(child) }, onPlay = { play(child, child.canResume) })
                        else SeasonRow(child, onOpen = { onOpenItem(child) })
                    }
                }
            }
        }
        IconButton(
            onClick = onBack,
            modifier = Modifier.windowInsetsPadding(WindowInsets.statusBars).padding(8.dp),
            colors = IconButtonDefaults.iconButtonColors(containerColor = Color.Black.copy(alpha = 0.4f), contentColor = Color.White),
        ) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Back") }
    }
}

@Composable
private fun Header(item: JfItem) {
    Box(Modifier.fillMaxWidth()) {
        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).heightIn(max = 360.dp)) {
            JfImage(item.backdropUrl ?: item.imageUrl, Modifier.fillMaxSize())
            Box(Modifier.fillMaxSize().background(Brush.verticalGradient(0.3f to Color.Transparent, 1f to MaterialTheme.colorScheme.background)))
        }
    }
    Row(Modifier.fillMaxWidth().overlapUp(56.dp).padding(horizontal = 20.dp), verticalAlignment = Alignment.Bottom) {
        Box(Modifier.width(110.dp).aspectRatio(2f / 3f).clip(RoundedCornerShape(12.dp))) {
            JfImage(item.imageUrl, Modifier.fillMaxSize())
            if (item.played) WatchedBadge(Modifier.align(Alignment.TopEnd).padding(6.dp))
        }
        Column(Modifier.padding(start = 16.dp).weight(1f)) {
            if (item.type == "Episode" || item.type == "Season") {
                item.seriesName?.let { Text(it, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary) }
            }
            Text(item.name, style = MaterialTheme.typography.headlineSmall, maxLines = 3, overflow = TextOverflow.Ellipsis)
            val meta = listOfNotNull(
                item.episodeLabel.takeIf { item.type == "Episode" },
                item.year?.toString(),
                formatRuntime(item.runtimeS),
                item.childCount?.let { if (item.type == "Series") "$it seasons" else if (item.type == "Season") "$it episodes" else null },
                if (item.played) "Watched" else null,
            ).joinToString(" · ")
            if (meta.isNotEmpty()) Text(meta, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 4.dp))
            item.progress?.takeIf { !item.played && it > 0f }?.let {
                ProgressOverlay(it, Modifier.padding(top = 8.dp).widthIn(max = 200.dp).clip(CircleShape))
            }
        }
    }
}

/** Pulls content up over the previous element (and shrinks its layout height accordingly). */
private fun Modifier.overlapUp(amount: Dp): Modifier = layout { measurable, constraints ->
    val placeable = measurable.measure(constraints)
    val px = amount.roundToPx()
    layout(placeable.width, (placeable.height - px).coerceAtLeast(0)) { placeable.place(0, -px) }
}

@Composable
private fun SeasonRow(item: JfItem, onOpen: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onOpen).padding(horizontal = 20.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.width(64.dp).aspectRatio(2f / 3f).clip(RoundedCornerShape(8.dp))) {
            JfImage(item.imageUrl, Modifier.fillMaxSize())
        }
        Column(Modifier.padding(start = 16.dp).weight(1f)) {
            Text(item.name, style = MaterialTheme.typography.titleSmall)
            item.childCount?.let { Text("$it episodes", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        if (item.played) WatchedBadge()
    }
    HorizontalDivider(Modifier.padding(start = 100.dp), color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.4f))
}

@Composable
private fun EpisodeRow(item: JfItem, onOpen: () -> Unit, onPlay: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onOpen).padding(horizontal = 20.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.width(140.dp).aspectRatio(16f / 9f).clip(RoundedCornerShape(10.dp))) {
            JfImage(item.imageUrl ?: item.backdropUrl, Modifier.fillMaxSize())
            if (item.played) WatchedBadge(Modifier.align(Alignment.TopEnd).padding(4.dp))
            ProgressOverlay(item.progress?.takeIf { !item.played }, Modifier.align(Alignment.BottomCenter))
        }
        Column(Modifier.padding(start = 14.dp).weight(1f)) {
            Text(
                listOfNotNull(item.episode?.let { "$it." }, item.name).joinToString(" "),
                style = MaterialTheme.typography.titleSmall, maxLines = 2, overflow = TextOverflow.Ellipsis,
                color = if (item.played) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface,
            )
            formatRuntime(item.runtimeS)?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        FilledTonalIconButton(onClick = onPlay, modifier = Modifier.size(44.dp)) { Icon(Icons.Rounded.PlayArrow, "Play ${item.name}") }
    }
}
