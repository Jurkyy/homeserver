package dev.woto.homeserver.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.VolumeUp
import androidx.compose.material.icons.rounded.Movie
import androidx.compose.material.icons.rounded.SettingsRemote
import androidx.compose.material.icons.rounded.Subscriptions
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.data.PairingInfo
import dev.woto.homeserver.ui.movies.JfDetailScreen
import dev.woto.homeserver.ui.movies.JfLibraryScreen
import dev.woto.homeserver.ui.movies.MoviesScreen
import dev.woto.homeserver.ui.pairing.PairingScreen
import dev.woto.homeserver.ui.pairing.ScannerScreen
import dev.woto.homeserver.ui.remote.RemoteScreen
import dev.woto.homeserver.ui.settings.SettingsScreen
import dev.woto.homeserver.ui.youtube.YouTubeScreen
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow

val LocalSnackbar = staticCompositionLocalOf { SnackbarHostState() }

object Routes {
    const val PAIR = "pair"
    const val SCAN = "scan"
    const val REMOTE = "remote"
    const val YOUTUBE = "youtube"
    const val MOVIES = "movies"
    const val SETTINGS = "settings"
    const val JF_ITEM = "jf/item/{id}"
    const val JF_LIBRARY = "jf/library/{id}?name={name}"
    fun jfItem(id: String) = "jf/item/${enc(id)}"
    fun jfLibrary(id: String, name: String) = "jf/library/${enc(id)}?name=${enc(name)}"
    private fun enc(s: String) = android.net.Uri.encode(s)
}

private data class Tab(val route: String, val label: String, val icon: ImageVector)

private val tabs = listOf(
    Tab(Routes.REMOTE, "Remote", Icons.Rounded.SettingsRemote),
    Tab(Routes.YOUTUBE, "YouTube", Icons.Rounded.Subscriptions),
    Tab(Routes.MOVIES, "Movies", Icons.Rounded.Movie),
)

@Composable
fun AppRoot(
    graph: AppGraph,
    pendingPair: StateFlow<PairingInfo?>,
    onPairConsumed: () -> Unit,
    volumeHud: StateFlow<Long>,
    onPaired: () -> Unit,
) {
    val nav = rememberNavController()
    val snackbar = remember { SnackbarHostState() }
    val pending by pendingPair.collectAsStateWithLifecycle()
    var scanned by remember { mutableStateOf<PairingInfo?>(null) }
    val startPaired = remember { graph.settings.current.isPaired }

    LaunchedEffect(Unit) {
        graph.actions.errors.collect { snackbar.showSnackbar(it) }
    }
    LaunchedEffect(pending) {
        if (pending != null && nav.currentDestination?.route != Routes.PAIR) nav.navigate(Routes.PAIR) { launchSingleTop = true }
    }

    val backStack by nav.currentBackStackEntryAsState()
    val route = backStack?.destination?.route
    val showNav = route in tabs.map { it.route }
    val wide = LocalConfiguration.current.screenWidthDp >= 600

    CompositionLocalProvider(LocalSnackbar provides snackbar) {
        Row(Modifier.fillMaxSize()) {
            if (showNav && wide) {
                NavigationRail(Modifier.windowInsetsPadding(WindowInsets.safeDrawing)) {
                    tabs.forEach { tab ->
                        NavigationRailItem(
                            selected = route == tab.route,
                            onClick = { nav.switchTab(tab.route) },
                            icon = { Icon(tab.icon, null) },
                            label = { Text(tab.label) },
                        )
                    }
                }
            }
            Scaffold(
                modifier = Modifier.weight(1f),
                snackbarHost = { SnackbarHost(snackbar) },
                bottomBar = {
                    if (showNav && !wide) {
                        NavigationBar {
                            tabs.forEach { tab ->
                                NavigationBarItem(
                                    selected = route == tab.route,
                                    onClick = { nav.switchTab(tab.route) },
                                    icon = { Icon(tab.icon, null) },
                                    label = { Text(tab.label) },
                                )
                            }
                        }
                    }
                },
                contentWindowInsets = WindowInsets(0),
            ) { padding ->
                Box(Modifier.fillMaxSize().padding(padding)) {
                    NavHost(nav, startDestination = if (startPaired) Routes.REMOTE else Routes.PAIR) {
                        composable(Routes.PAIR) {
                            PairingScreen(
                                graph = graph,
                                prefill = pending ?: scanned,
                                onPrefillConsumed = { onPairConsumed(); scanned = null },
                                onScan = { nav.navigate(Routes.SCAN) },
                                canGoBack = nav.previousBackStackEntry != null,
                                onBack = { nav.popBackStack() },
                                onPaired = {
                                    onPaired()
                                    nav.navigate(Routes.REMOTE) { popUpTo(0) { inclusive = true } }
                                },
                            )
                        }
                        composable(Routes.SCAN) {
                            ScannerScreen(
                                onResult = { scanned = it; nav.popBackStack() },
                                onBack = { nav.popBackStack() },
                            )
                        }
                        composable(Routes.REMOTE) {
                            RemoteScreen(graph, onOpenSettings = { nav.navigate(Routes.SETTINGS) })
                        }
                        composable(Routes.YOUTUBE) { YouTubeScreen(graph) }
                        composable(Routes.MOVIES) {
                            MoviesScreen(
                                graph,
                                onOpenItem = { item ->
                                    if (item.isFolderLike) nav.navigate(Routes.jfLibrary(item.id, item.name)) else nav.navigate(Routes.jfItem(item.id))
                                },
                            )
                        }
                        composable(Routes.SETTINGS) {
                            SettingsScreen(graph, onBack = { nav.popBackStack() }, onRepair = { nav.navigate(Routes.PAIR) })
                        }
                        composable(Routes.JF_ITEM, arguments = listOf(navArgument("id") { type = NavType.StringType })) { entry ->
                            JfDetailScreen(
                                graph = graph,
                                id = entry.arguments?.getString("id").orEmpty(),
                                onBack = { nav.popBackStack() },
                                onOpenItem = { nav.navigate(Routes.jfItem(it.id)) },
                            )
                        }
                        composable(
                            Routes.JF_LIBRARY,
                            arguments = listOf(
                                navArgument("id") { type = NavType.StringType },
                                navArgument("name") { type = NavType.StringType; defaultValue = "" },
                            ),
                        ) { entry ->
                            JfLibraryScreen(
                                graph = graph,
                                parentId = entry.arguments?.getString("id").orEmpty(),
                                title = entry.arguments?.getString("name").orEmpty(),
                                onBack = { nav.popBackStack() },
                                onOpenItem = { item ->
                                    if (item.isFolderLike) nav.navigate(Routes.jfLibrary(item.id, item.name)) else nav.navigate(Routes.jfItem(item.id))
                                },
                            )
                        }
                    }
                    VolumeHud(graph, volumeHud, Modifier.align(Alignment.TopCenter))
                }
            }
        }
    }
}

private fun NavHostController.switchTab(route: String) {
    navigate(route) {
        popUpTo(Routes.REMOTE) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}

@Composable
private fun VolumeHud(graph: AppGraph, trigger: StateFlow<Long>, modifier: Modifier) {
    val bump by trigger.collectAsStateWithLifecycle()
    val snap by graph.repository.snapshot.collectAsStateWithLifecycle()
    var visible by remember { mutableStateOf(false) }
    LaunchedEffect(bump) {
        if (bump == 0L) return@LaunchedEffect
        visible = true
        delay(1500)
        visible = false
    }
    AnimatedVisibility(
        visible, modifier.windowInsetsPadding(WindowInsets.safeDrawing).padding(top = 8.dp),
        enter = fadeIn() + slideInVertically(), exit = fadeOut() + slideOutVertically(),
    ) {
        Surface(shape = RoundedCornerShape(24.dp), tonalElevation = 6.dp, shadowElevation = 6.dp) {
            Row(Modifier.padding(horizontal = 16.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.AutoMirrored.Rounded.VolumeUp, null, Modifier.size(20.dp), tint = MaterialTheme.colorScheme.primary)
                LinearProgressIndicator(
                    progress = { ((snap?.state?.volume ?: 0.0) / 100.0).toFloat() },
                    modifier = Modifier.padding(horizontal = 12.dp).width(140.dp),
                )
                Text("${(snap?.state?.volume ?: 0.0).toInt()}", style = MaterialTheme.typography.labelLarge)
            }
        }
    }
}
