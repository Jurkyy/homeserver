package dev.woto.homeserver.ui.youtube

import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
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
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.ArrowUpward
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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
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
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class FeedUi(
    val items: List<YouTubeVideo> = emptyList(),
    val loading: Boolean = true,
    val refreshing: Boolean = false,
    val error: String? = null,
    /** Server `changed_at` of the loaded feed (0 = unknown). */
    val changedAt: Long = 0,
    /** Ids that arrived via a background refresh and the user hasn't scrolled up to yet. */
    val newIds: Set<String> = emptySet(),
)

class YouTubeViewModel(private val graph: AppGraph) : ViewModel() {
    private val _ui = MutableStateFlow(FeedUi())
    val ui: StateFlow<FeedUi> = _ui.asStateFlow()

    private var loadJob: Job? = null
    private var backgroundJob: Job? = null
    private var lastHandledSignal = 0L

    init {
        load(refresh = false)
        // The server sends `event: feed` on every SSE (re)connect — including when the app returns to the
        // foreground — and whenever its background refresh changes the feed.
        viewModelScope.launch { graph.repository.feedSignal.collect { maybeBackgroundRefresh() } }
    }

    fun load(refresh: Boolean) {
        backgroundJob?.cancel()
        loadJob?.cancel()
        _ui.value = _ui.value.copy(loading = _ui.value.items.isEmpty(), refreshing = refresh, error = null)
        loadJob = viewModelScope.launch {
            runCatching { graph.api.youtubeFeed(refresh = refresh, shorts = true) }
                .onSuccess { _ui.value = FeedUi(items = it.items, loading = false, changedAt = it.changedAt) }
                .onFailure {
                    if (it is CancellationException) throw it
                    _ui.value = _ui.value.copy(loading = false, refreshing = false, error = it.userMessage())
                }
            loadJob = null
            maybeBackgroundRefresh()
        }
    }

    /** The user saw the new items (scrolled to the top or tapped the pill). */
    fun clearNew() {
        if (_ui.value.newIds.isNotEmpty()) _ui.value = _ui.value.copy(newIds = emptySet())
    }

    /** Quietly refetches (no spinner) if the server's latest feed signal is newer than what we show. */
    private fun maybeBackgroundRefresh() {
        val signal = graph.repository.feedSignal.value?.updatedAt ?: return
        val cur = _ui.value
        if (cur.loading || cur.refreshing || loadJob?.isActive == true || backgroundJob?.isActive == true) return
        if (!FeedRefresh.shouldRefetch(signal, cur.changedAt, lastHandledSignal)) return
        lastHandledSignal = signal
        backgroundJob = viewModelScope.launch {
            runCatching { graph.api.youtubeFeed(refresh = false, shorts = true) }
                .onSuccess { feed ->
                    val prev = _ui.value
                    val newIds = FeedRefresh.pendingNewIds(prev.items.map { it.id }, feed.items.map { it.id }, prev.newIds)
                    _ui.value = prev.copy(items = feed.items, error = null, changedAt = feed.changedAt, newIds = newIds)
                }
                .onFailure { if (it is CancellationException) throw it } // silent; the next signal retries
            backgroundJob = null
            maybeBackgroundRefresh() // a newer signal may have arrived meanwhile
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

    // Grid state lives outside the `when` so background refreshes never reset it. Items are keyed by video id, so
    // when new videos are prepended the grid stays anchored on what the user is looking at.
    val gridState = rememberLazyGridState()
    val atTop by remember { derivedStateOf { gridState.firstVisibleItemIndex == 0 && gridState.firstVisibleItemScrollOffset == 0 } }
    // If the user was already at the top, show the newly prepended videos instead of anchoring below them.
    val firstId = visible.firstOrNull()?.id
    val lastFirstId = remember { arrayOfNulls<String>(1) }
    if (firstId != lastFirstId[0]) {
        if (atTop && firstId != null) gridState.requestScrollToItem(0)
        lastFirstId[0] = firstId
    }
    val newVisible = remember(visible, ui.newIds) { if (ui.newIds.isEmpty()) 0 else visible.count { it.id in ui.newIds } }
    LaunchedEffect(atTop, ui.newIds) { if (atTop && ui.newIds.isNotEmpty()) vm.clearNew() }

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
                    state = gridState,
                    columns = GridCells.Adaptive(minSize = 300.dp),
                    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 24.dp),
                    horizontalArrangement = Arrangement.spacedBy(16.dp),
                    verticalArrangement = Arrangement.spacedBy(18.dp),
                    modifier = Modifier.fillMaxSize(),
                ) {
                    items(visible, key = { it.id }) { v -> VideoCard(v, onClick = { cast(v) }) }
                }
            }
            androidx.compose.animation.AnimatedVisibility( // unscoped: we are in a Box, not the outer Column
                visible = newVisible > 0 && !atTop,
                enter = fadeIn() + slideInVertically { -it },
                exit = fadeOut() + slideOutVertically { -it },
                modifier = Modifier.align(Alignment.TopCenter).padding(top = 8.dp),
            ) {
                NewVideosPill(newVisible) {
                    scope.launch { gridState.scrollToItem(0) }
                    vm.clearNew()
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
private fun NewVideosPill(count: Int, onClick: () -> Unit) {
    Surface(
        onClick = onClick,
        shape = RoundedCornerShape(50),
        color = MaterialTheme.colorScheme.primary,
        contentColor = MaterialTheme.colorScheme.onPrimary,
        shadowElevation = 6.dp,
    ) {
        Row(Modifier.padding(horizontal = 14.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Rounded.ArrowUpward, null, Modifier.size(18.dp))
            Text(
                if (count == 1) "1 new video" else "$count new videos",
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(start = 6.dp),
            )
        }
    }
}

@Composable
private fun Badge(text: String, color: Color) {
    Surface(shape = RoundedCornerShape(6.dp), color = color) {
        Text(text, style = MaterialTheme.typography.labelSmall, color = Color.White, modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp))
    }
}
