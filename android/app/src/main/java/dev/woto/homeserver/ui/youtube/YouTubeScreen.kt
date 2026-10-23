package dev.woto.homeserver.ui.youtube

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Cast
import androidx.compose.material.icons.rounded.Clear
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material.icons.rounded.Subscriptions
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import coil3.compose.AsyncImage
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.data.YouTubeVideo
import dev.woto.homeserver.data.userMessage
import dev.woto.homeserver.ui.LocalSnackbar
import dev.woto.homeserver.ui.components.LoadingState
import dev.woto.homeserver.ui.components.MessageState
import dev.woto.homeserver.ui.components.formatDuration
import dev.woto.homeserver.ui.components.relativeTime
import dev.woto.homeserver.ui.components.rememberHaptic
import dev.woto.homeserver.ui.components.rememberImageUrl
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class FeedUi(
    val items: List<YouTubeVideo> = emptyList(),
    val loading: Boolean = true,
    val refreshing: Boolean = false,
    val error: String? = null,
)

class YouTubeViewModel(private val graph: AppGraph) : ViewModel() {
    private val _ui = MutableStateFlow(FeedUi())
    val ui: StateFlow<FeedUi> = _ui.asStateFlow()

    init { load(refresh = false) }

    fun load(refresh: Boolean) {
        _ui.value = _ui.value.copy(loading = _ui.value.items.isEmpty(), refreshing = refresh, error = null)
        viewModelScope.launch {
            runCatching { graph.api.youtubeFeed(refresh = refresh, shorts = true) }
                .onSuccess { _ui.value = FeedUi(items = it.items, loading = false) }
                .onFailure { _ui.value = _ui.value.copy(loading = false, refreshing = false, error = it.userMessage()) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun YouTubeScreen(graph: AppGraph) {
    val vm: YouTubeViewModel = viewModel { YouTubeViewModel(graph) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    val settings by graph.settings.settings.collectAsStateWithLifecycle()
    var query by remember { mutableStateOf("") }
    var channel by remember { mutableStateOf<String?>(null) }
    val snackbar = LocalSnackbar.current
    val scope = rememberCoroutineScope()
    val haptic = rememberHaptic()

    val channels = remember(ui.items) {
        ui.items.groupingBy { it.channel }.eachCount().entries.sortedByDescending { it.value }.map { it.key }.filter { it.isNotBlank() }
    }
    val visible = remember(ui.items, query, channel, settings.hideShorts) {
        val q = query.trim()
        ui.items.filter { v ->
            (!settings.hideShorts || !v.isShort) &&
                (channel == null || v.channel == channel) &&
                (q.isEmpty() || v.title.contains(q, true) || v.channel.contains(q, true))
        }
    }

    fun cast(v: YouTubeVideo) {
        haptic(true)
        scope.launch {
            snackbar.currentSnackbarData?.dismiss()
            graph.actions.cast(v.url)
                .onSuccess { snackbar.showSnackbar("Casting “${v.title}”") }
                .onFailure { snackbar.showSnackbar("Couldn't cast: ${it.userMessage()}") }
        }
    }

    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Subscriptions") },
            actions = {
                if (ui.refreshing) CircularProgressIndicator(Modifier.size(24.dp).padding(2.dp), strokeWidth = 2.dp)
                else IconButton(onClick = { vm.load(refresh = true) }) { Icon(Icons.Rounded.Refresh, "Refresh") }
            },
            windowInsets = WindowInsets.statusBars,
        )
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            placeholder = { Text("Search titles and channels") },
            leadingIcon = { Icon(Icons.Rounded.Search, null) },
            trailingIcon = { if (query.isNotEmpty()) IconButton(onClick = { query = "" }) { Icon(Icons.Rounded.Clear, "Clear") } },
            singleLine = true,
            shape = RoundedCornerShape(28.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp),
        )
        LazyRow(
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            item {
                FilterChip(
                    selected = settings.hideShorts,
                    onClick = { scope.launch { graph.settings.setHideShorts(!settings.hideShorts) } },
                    label = { Text("Hide Shorts") },
                )
            }
            items(channels, key = { it }) { ch ->
                FilterChip(selected = channel == ch, onClick = { channel = if (channel == ch) null else ch }, label = { Text(ch) })
            }
        }
        PullToRefreshBox(isRefreshing = ui.refreshing, onRefresh = { vm.load(refresh = true) }, modifier = Modifier.fillMaxSize()) {
            when {
                ui.loading -> LoadingState()
                ui.error != null && ui.items.isEmpty() -> MessageState(
                    "Couldn't load the feed", body = ui.error, actionLabel = "Retry", onAction = { vm.load(refresh = false) },
                )
                visible.isEmpty() -> MessageState(
                    if (ui.items.isEmpty()) "No videos yet" else "No matches",
                    body = if (ui.items.isEmpty()) "Pull to refresh the subscriptions feed." else "Try a different search or channel.",
                    icon = Icons.Rounded.Subscriptions,
                )
                else -> LazyVerticalGrid(
                    columns = GridCells.Adaptive(minSize = 300.dp),
                    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 24.dp),
                    horizontalArrangement = Arrangement.spacedBy(16.dp),
                    verticalArrangement = Arrangement.spacedBy(18.dp),
                    modifier = Modifier.fillMaxSize(),
                ) {
                    items(visible, key = { it.id }) { v -> VideoCard(v, onClick = { cast(v) }) }
                }
            }
        }
    }
}

@Composable
private fun VideoCard(v: YouTubeVideo, onClick: () -> Unit) {
    val thumb = rememberImageUrl(v.thumbnail)
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).clickable(onClick = onClick)) {
        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(16.dp)).background(MaterialTheme.colorScheme.surfaceContainerHigh)) {
            AsyncImage(thumb, null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
            Row(Modifier.align(Alignment.BottomEnd).padding(8.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                if (v.isShort) Badge("SHORT", MaterialTheme.colorScheme.error.copy(alpha = 0.9f))
                v.durationS?.let { Badge(formatDuration(it), Color.Black.copy(alpha = 0.75f)) }
            }
            Surface(
                Modifier.align(Alignment.TopEnd).padding(8.dp).size(32.dp),
                shape = RoundedCornerShape(16.dp), color = Color.Black.copy(alpha = 0.5f),
            ) {
                Icon(Icons.Rounded.Cast, "Cast", Modifier.padding(7.dp), tint = Color.White)
            }
        }
        Text(
            v.title, style = MaterialTheme.typography.titleSmall, maxLines = 2, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 8.dp, start = 4.dp, end = 4.dp),
        )
        Text(
            listOfNotNull(v.channel.ifBlank { null }, relativeTime(v.published)).joinToString(" · "),
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp),
        )
    }
}

@Composable
private fun Badge(text: String, color: Color) {
    Surface(shape = RoundedCornerShape(6.dp), color = color) {
        Text(text, style = MaterialTheme.typography.labelSmall, color = Color.White, modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp))
    }
}
