package dev.woto.homeserver.data

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.launch

/** Fire-and-forget commands with optimistic UI updates; failures are surfaced on [errors]. */
class RemoteActions(
    private val api: ApiClient,
    private val repo: StateRepository,
    private val scope: CoroutineScope,
) {
    private val _errors = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val errors: SharedFlow<String> = _errors

    private fun launch(block: suspend () -> Unit) = scope.launch {
        runCatching { block() }.onFailure { _errors.tryEmit(it.userMessage()) }
    }

    fun togglePlay() {
        repo.optimistic { s ->
            when {
                s.isPlaying -> s.copy(activity = "paused")
                s.isPaused -> s.copy(activity = "playing")
                else -> s
            }
        }
        launch { api.control("toggle") }
    }

    fun play() = launch { api.control("play") }
    fun pause() = launch { api.control("pause") }

    fun stop() {
        repo.optimistic { it.copy(activity = "idle") }
        launch { api.control("stop") }
    }

    fun seekBy(seconds: Double) {
        val pos = repo.snapshot.value?.positionAt()
        if (pos != null) repo.optimistic { s -> s.copy(positionS = (pos + seconds).coerceAtLeast(0.0)) }
        launch { api.control("seek", seconds) }
    }

    fun seekTo(seconds: Double) {
        repo.optimistic { it.copy(positionS = seconds) }
        launch { api.control("seek_to", seconds) }
    }

    fun setVolume(volume: Double) {
        val v = volume.coerceIn(0.0, 100.0)
        repo.optimistic { it.copy(volume = v) }
        launch { api.control("volume", v) }
    }

    fun volumeDelta(delta: Double) {
        repo.optimistic { it.copy(volume = (it.volume + delta).coerceIn(0.0, 100.0)) }
        launch { api.control("volume_delta", delta) }
    }

    // --- Spotify (librespot on the host) -------------------------------------------------------------------

    private fun spotifyOptimistic(transform: (SpotifyState) -> SpotifyState) =
        repo.optimistic { s -> s.spotify?.let { s.copy(spotify = transform(it)) } ?: s }

    fun spotifyToggle() {
        spotifyOptimistic { it.rebased(playing = !it.playing) }
        launch { api.spotifyControl("toggle") }
    }

    fun spotifyPlay() {
        spotifyOptimistic { it.rebased(playing = true) }
        launch { api.spotifyControl("play") }
    }

    fun spotifyPause() {
        spotifyOptimistic { it.rebased(playing = false) }
        launch { api.spotifyControl("pause") }
    }

    fun spotifyNext() = launch { api.spotifyControl("next") }
    fun spotifyPrevious() = launch { api.spotifyControl("previous") }

    fun spotifySeekTo(seconds: Double) {
        val s = seconds.coerceAtLeast(0.0)
        spotifyOptimistic { it.rebased(positionS = s) }
        launch { api.spotifyControl("seek_to", s) }
    }

    fun spotifySetVolume(volume: Double) {
        val v = volume.coerceIn(0.0, 100.0)
        spotifyOptimistic { it.copy(volume = v) }
        launch { api.spotifyControl("volume", v) }
    }

    fun spotifyVolumeDelta(delta: Double) {
        spotifyOptimistic { sp -> sp.copy(volume = sp.volume?.let { (it + delta).coerceIn(0.0, 100.0) }) }
        launch { api.spotifyControl("volume_delta", delta) }
    }

    fun screensaver(theme: String?) = launch { api.screensaver(theme) }

    fun power(action: String) {
        when (action) {
            "sleep" -> repo.optimistic { it.copy(power = "sleeping", activity = "idle") }
            "wake" -> repo.optimistic { it.copy(power = "on", screen = "on") }
        }
        launch { api.power(action) }
    }

    suspend fun cast(url: String): Result<Unit> = runCatching { api.cast(url); Unit }
    suspend fun playJellyfin(id: String, resume: Boolean, lowQuality: Boolean = false): Result<Unit> =
        runCatching { api.jfPlay(id, resume, lowQuality); Unit }
}
