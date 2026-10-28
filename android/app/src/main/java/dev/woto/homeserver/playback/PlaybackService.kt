package dev.woto.homeserver.playback

import android.app.PendingIntent
import android.content.Intent
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import dev.woto.homeserver.MainActivity
import dev.woto.homeserver.data.MediaTarget
import dev.woto.homeserver.graph
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * Media notification + lock-screen controls mirroring the projector. Runs only while something is loaded
 * on the projector or a controllable Spotify session is active ([dev.woto.homeserver.data.ServerState.mediaTarget])
 * and the setting is enabled; stops itself when neither is the case any more.
 */
@UnstableApi
class PlaybackService : MediaSessionService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var session: MediaSession? = null
    private var player: RemotePlayer? = null
    private var stopJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        running = true
        val g = graph
        g.repository.acquire(HOLD_KEY)
        val p = RemotePlayer(mainLooper, { g.repository.snapshot.value }, { g.settings.current.baseUrl }, g.actions)
        player = p
        val openApp = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        session = MediaSession.Builder(this, p)
            .setId("projector")
            .setSessionActivity(openApp)
            .build()

        scope.launch {
            combine(g.repository.snapshot, g.settings.settings) { snap, settings -> snap to settings }.collect { (snap, settings) ->
                p.refresh()
                val s = snap?.state
                val keep = settings.mediaNotification && settings.isPaired && (s == null || s.mediaTarget != MediaTarget.None)
                if (keep) {
                    stopJob?.cancel(); stopJob = null
                } else if (stopJob == null) {
                    // Small grace period so a quick stop → cast transition doesn't flicker the notification.
                    stopJob = scope.launch {
                        delay(if (s?.isSleeping == true || !settings.mediaNotification) 0 else 4_000)
                        shutdown()
                    }
                }
            }
        }
    }

    private fun shutdown() {
        session?.let { removeSession(it) }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        val s = graph.repository.state
        val playing = when (s?.mediaTarget) {
            MediaTarget.Cast -> s.isPlaying
            MediaTarget.Spotify -> s.controllableSpotify?.playing == true
            else -> false
        }
        if (!playing) shutdown()
    }

    override fun onDestroy() {
        running = false
        scope.cancel()
        graph.repository.release(HOLD_KEY)
        session?.release()
        session = null
        player?.release()
        player = null
        super.onDestroy()
    }

    companion object {
        private const val HOLD_KEY = "media-notification"

        @Volatile
        var running: Boolean = false
            private set
    }
}
