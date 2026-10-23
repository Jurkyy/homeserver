package dev.woto.homeserver.ui.movies

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.KeyboardArrowRight
import androidx.compose.material.icons.rounded.Clear
import androidx.compose.material.icons.rounded.Movie
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.data.JfItem
import dev.woto.homeserver.data.userMessage
import dev.woto.homeserver.ui.components.LoadingState
import dev.woto.homeserver.ui.components.MessageState
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class LibraryRow(val view: JfItem, val items: List<JfItem>)

data class MoviesUi(
    val loading: Boolean = true,
    val refreshing: Boolean = false,
    val error: String? = null,
    val resume: List<JfItem> = emptyList(),
    val nextUp: List<JfItem> = emptyList(),
    val libraries: List<LibraryRow> = emptyList(),
    val searchResults: List<JfItem>? = null,
    val searching: Boolean = false,
)

class MoviesViewModel(private val graph: AppGraph) : ViewModel() {
    private val _ui = MutableStateFlow(MoviesUi())
    val ui: StateFlow<MoviesUi> = _ui.asStateFlow()
    private var searchJob: Job? = null

    init { load() }

    fun load(refresh: Boolean = false) {
        _ui.value = _ui.value.copy(loading = !refresh && _ui.value.libraries.isEmpty(), refreshing = refresh, error = null)
        viewModelScope.launch {
            val api = graph.api
            runCatching {
                val resume = async { runCatching { api.jfResume().items }.getOrDefault(emptyList()) }
                val nextUp = async { runCatching { api.jfNextUp().items }.getOrDefault(emptyList()) }
                val views = api.jfViews().items
                val rows = views.map { v ->
                    async { LibraryRow(v, runCatching { api.jfItems(parentId = v.id, limit = 24).items }.getOrDefault(emptyList())) }
                }.awaitAll()
                MoviesUi(loading = false, resume = resume.await(), nextUp = nextUp.await(), libraries = rows, searchResults = _ui.value.searchResults)
            }.onSuccess { _ui.value = it }
                .onFailure { _ui.value = _ui.value.copy(loading = false, refreshing = false, error = it.userMessage()) }
        }
    }

    fun search(query: String) {
        searchJob?.cancel()
        val q = query.trim()
        if (q.isEmpty()) {
            _ui.value = _ui.value.copy(searchResults = null, searching = false)
            return
        }
        _ui.value = _ui.value.copy(searching = true)
        searchJob = viewModelScope.launch {
            delay(300) // debounce
            runCatching { graph.api.jfItems(search = q, limit = 60, types = "Movie,Series,Episode").items }
                .onSuccess { _ui.value = _ui.value.copy(searchResults = it, searching = false) }
                .onFailure { _ui.value = _ui.value.copy(searchResults = emptyList(), searching = false, error = it.userMessage()) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MoviesScreen(graph: AppGraph, onOpenItem: (JfItem) -> Unit) {
    val vm: MoviesViewModel = viewModel { MoviesViewModel(graph) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    var query by rememberSaveable { mutableStateOf("") }
    val focus = LocalFocusManager.current
    LaunchedEffect(query) { vm.search(query) }

    Column(Modifier.fillMaxSize()) {
        TopAppBar(title = { Text("mrpflix") }, windowInsets = WindowInsets.statusBars)
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            placeholder = { Text("Search movies and shows") },
            leadingIcon = { Icon(Icons.Rounded.Search, null) },
            trailingIcon = { if (query.isNotEmpty()) IconButton(onClick = { query = "" }) { Icon(Icons.Rounded.Clear, "Clear") } },
            singleLine = true,
            shape = RoundedCornerShape(28.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(onSearch = { focus.clearFocus() }),
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(bottom = 8.dp),
        )
        val results = ui.searchResults
        when {
            query.isNotBlank() -> when {
                results == null || (ui.searching && results.isEmpty()) -> LoadingState()
                results.isEmpty() -> MessageState("Nothing found", body = "No movies or shows match “$query”.", icon = Icons.Rounded.Search)
                else -> PosterGrid(results, onOpenItem)
            }
            ui.loading -> LoadingState()
            ui.error != null && ui.libraries.isEmpty() -> MessageState(
                "Couldn't load mrpflix", body = ui.error, actionLabel = "Retry", onAction = { vm.load() },
            )
            else -> PullToRefreshBox(isRefreshing = ui.refreshing, onRefresh = { vm.load(refresh = true) }, modifier = Modifier.fillMaxSize()) {
                LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (ui.resume.isNotEmpty()) {
                        item(key = "resume") {
                            Shelf("Continue watching") {
                                LazyRow(contentPadding = PaddingValues(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                    items(ui.resume, key = { it.id }) { WideCard(it, onClick = { onOpenItem(it) }) }
                                }
                            }
                        }
                    }
                    if (ui.nextUp.isNotEmpty()) {
                        item(key = "nextup") {
                            Shelf("Next up") {
                                LazyRow(contentPadding = PaddingValues(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                    items(ui.nextUp, key = { it.id }) { WideCard(it, onClick = { onOpenItem(it) }) }
                                }
                            }
                        }
                    }
                    ui.libraries.forEach { row ->
                        item(key = "lib-${row.view.id}") {
                            Shelf(row.view.name, onMore = { onOpenItem(row.view) }) {
                                if (row.items.isEmpty()) {
                                    Text("Empty", Modifier.padding(horizontal = 16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
                                } else {
                                    LazyRow(contentPadding = PaddingValues(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                        items(row.items, key = { it.id }) { PosterCard(it, onClick = { onOpenItem(it) }) }
                                    }
                                }
                            }
                        }
                    }
                    if (ui.resume.isEmpty() && ui.nextUp.isEmpty() && ui.libraries.isEmpty()) {
                        item { MessageState("No libraries", body = "The Jellyfin server returned no libraries.", icon = Icons.Rounded.Movie) }
                    }
                }
            }
        }
    }
}

@Composable
fun PosterGrid(items: List<JfItem>, onOpenItem: (JfItem) -> Unit, footer: (@Composable () -> Unit)? = null) {
    LazyVerticalGrid(
        columns = GridCells.Adaptive(118.dp),
        contentPadding = PaddingValues(16.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
        modifier = Modifier.fillMaxSize(),
    ) {
        items(items, key = { it.id }) { PosterCard(it, onClick = { onOpenItem(it) }, width = null) }
        if (footer != null) item(span = { androidx.compose.foundation.lazy.grid.GridItemSpan(maxLineSpan) }) { footer() }
    }
}

@Composable
private fun Shelf(title: String, onMore: (() -> Unit)? = null, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 4.dp, top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.weight(1f))
            if (onMore != null) {
                TextButton(onClick = onMore) {
                    Text("See all")
                    Icon(Icons.AutoMirrored.Rounded.KeyboardArrowRight, null)
                }
            }
        }
        Spacer(Modifier.padding(top = 4.dp))
        content()
    }
}
