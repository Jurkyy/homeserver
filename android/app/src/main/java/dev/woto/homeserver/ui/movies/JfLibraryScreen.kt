package dev.woto.homeserver.ui.movies

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.Movie
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
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
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class LibraryUi(
    val items: List<JfItem> = emptyList(),
    val total: Int? = null,
    val loading: Boolean = true,
    val loadingMore: Boolean = false,
    val error: String? = null,
) {
    val canLoadMore: Boolean get() = !loading && !loadingMore && error == null && (total == null || items.size < total)
}

class LibraryViewModel(private val graph: AppGraph, private val parentId: String) : ViewModel() {
    private val _ui = MutableStateFlow(LibraryUi())
    val ui: StateFlow<LibraryUi> = _ui.asStateFlow()
    private var exhausted = false

    init { loadMore() }

    fun loadMore() {
        val cur = _ui.value
        if (exhausted || cur.loadingMore || (cur.items.isNotEmpty() && !cur.canLoadMore)) return
        _ui.value = cur.copy(loadingMore = cur.items.isNotEmpty(), loading = cur.items.isEmpty(), error = null)
        viewModelScope.launch {
            runCatching { graph.api.jfItems(parentId = parentId, start = cur.items.size, limit = PAGE) }
                .onSuccess { page ->
                    if (page.items.size < PAGE) exhausted = true
                    val merged = (_ui.value.items + page.items).distinctBy { it.id }
                    _ui.value = LibraryUi(items = merged, total = page.total, loading = false)
                }
                .onFailure { _ui.value = _ui.value.copy(loading = false, loadingMore = false, error = it.userMessage()) }
        }
    }

    fun retry() {
        _ui.value = _ui.value.copy(error = null)
        loadMore()
    }

    companion object { const val PAGE = 60 }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun JfLibraryScreen(graph: AppGraph, parentId: String, title: String, onBack: () -> Unit, onOpenItem: (JfItem) -> Unit) {
    val vm: LibraryViewModel = viewModel(key = "lib-$parentId") { LibraryViewModel(graph, parentId) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(title.ifBlank { "Library" }) },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Back") } },
            windowInsets = WindowInsets.statusBars,
        )
        when {
            ui.loading -> LoadingState()
            ui.error != null && ui.items.isEmpty() -> MessageState("Couldn't load", body = ui.error, actionLabel = "Retry", onAction = vm::retry)
            ui.items.isEmpty() -> MessageState("This library is empty", icon = Icons.Rounded.Movie)
            else -> PosterGrid(ui.items, onOpenItem) {
                LaunchedEffect(ui.items.size) { vm.loadMore() }
                if (ui.loadingMore) {
                    Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                }
            }
        }
    }
}
