package dev.woto.homeserver

import android.app.Application
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.annotation.OptIn
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.media3.common.util.UnstableApi
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import coil3.request.crossfade
import dev.woto.homeserver.data.ApiClient
import dev.woto.homeserver.data.RemoteActions
import dev.woto.homeserver.data.SettingsStore
import dev.woto.homeserver.data.StateRepository
import dev.woto.homeserver.playback.PlaybackService
import dev.woto.homeserver.widget.NowPlayingWidget
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

/** Manual dependency graph. */
class AppGraph(context: Context) {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val settings = SettingsStore(context, scope)
    val http = ApiClient.defaultHttp()
    val api = ApiClient(http) { settings.current.credentials }
    val repository = StateRepository(api, settings, scope)
    val actions = RemoteActions(api, repository, scope)
}

@OptIn(UnstableApi::class)
class HomeserverApp : Application(), SingletonImageLoader.Factory {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = AppGraph(this)

        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) = graph.repository.acquire(KEY_FOREGROUND)
            override fun onStop(owner: LifecycleOwner) = graph.repository.release(KEY_FOREGROUND)
        })

        // Start the media notification service whenever something is loaded on the projector.
        graph.scope.launch {
            combine(graph.repository.snapshot, graph.settings.settings) { snap, s ->
                s.mediaNotification && s.isPaired && snap != null && snap.state.hasMedia && !snap.state.isSleeping
            }.distinctUntilChanged().collect { wanted ->
                if (wanted && !PlaybackService.running) startPlaybackService()
            }
        }

        // Keep the home-screen widget in sync with what's on.
        graph.scope.launch {
            graph.repository.snapshot.filterNotNull()
                .map { NowPlayingWidget.Key.of(it.state) }
                .distinctUntilChanged()
                .collect { NowPlayingWidget.updateAll(this@HomeserverApp, it) }
        }
    }

    private fun startPlaybackService() {
        try {
            startService(Intent(this, PlaybackService::class.java))
        } catch (e: Exception) {
            // Background-start restrictions (Android 12+): we'll retry next time the app is in the foreground.
            Log.w("HomeserverApp", "Could not start playback service: ${e.message}")
        }
    }

    override fun newImageLoader(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .components { add(OkHttpNetworkFetcherFactory(callFactory = { graph.http })) }
            .crossfade(true)
            .build()

    companion object {
        const val KEY_FOREGROUND = "foreground"
    }
}

val Context.graph: AppGraph get() = (applicationContext as HomeserverApp).graph
