package dev.woto.homeserver.data

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Projector/server state as described in docs/api-v1.md. Every field is defensive: the backend may evolve. */
@Serializable
data class ServerState(
    val power: String = "on",
    val screen: String = "on",
    val activity: String = "idle",
    val source: String? = null,
    val title: String? = null,
    val subtitle: String? = null,
    @SerialName("artwork_url") val artworkUrl: String? = null,
    @SerialName("position_s") val positionS: Double? = null,
    @SerialName("duration_s") val durationS: Double? = null,
    val volume: Double = 0.0,
    val muted: Boolean = false,
    val item: PlayingItem? = null,
    val screensaver: ScreensaverState? = null,
    val spotify: SpotifyState? = null,
    @SerialName("updated_at") val updatedAt: Long = 0,
) {
    val isSleeping: Boolean get() = power == "sleeping"
    val isPlaying: Boolean get() = activity == "playing"
    val isPaused: Boolean get() = activity == "paused"
    /** Something is loaded in the cast player (playing or paused). */
    val hasMedia: Boolean get() = activity == "playing" || activity == "paused"
}

@Serializable
data class PlayingItem(val kind: String? = null, val id: String? = null)

@Serializable
data class ScreensaverState(
    val active: Boolean = false,
    val theme: String? = null,
    val themes: List<ScreensaverTheme> = emptyList(),
    @SerialName("idle_minutes") val idleMinutes: Double? = null,
)

@Serializable
data class ScreensaverTheme(val id: String, val name: String? = null) {
    val label: String get() = name ?: id
}

@Serializable
data class SpotifyState(
    val active: Boolean = false,
    val playing: Boolean = false,
    val title: String? = null,
    val artist: String? = null,
    val album: String? = null,
    @SerialName("artwork_url") val artworkUrl: String? = null,
    @SerialName("position_s") val positionS: Double? = null,
    @SerialName("duration_s") val durationS: Double? = null,
    @SerialName("updated_at") val updatedAt: Long = 0,
) {
    /** librespot only reports on play/pause/seek/track change, so extrapolate from [updatedAt] (wall clock). */
    fun positionAt(nowMs: Long = System.currentTimeMillis()): Double? {
        val p = positionS ?: return null
        val pos = if (playing && updatedAt > 0) p + (nowMs - updatedAt).coerceAtLeast(0) / 1000.0 else p
        val d = durationS
        return if (d != null && d > 0) pos.coerceIn(0.0, d) else pos.coerceAtLeast(0.0)
    }
}

@Serializable
data class OkResponse(val ok: Boolean = false)

@Serializable
data class ErrorResponse(val error: String? = null, val code: String? = null)

@Serializable
data class HealthResponse(val ok: Boolean = false, val version: String? = null)

@Serializable
data class YouTubeFeed(
    val items: List<YouTubeVideo> = emptyList(),
    @SerialName("updated_at") val updatedAt: Long = 0,
    /** Unix ms of the last time the feed content changed; 0 = unknown (older servers). */
    @SerialName("changed_at") val changedAt: Long = 0,
)

/** Payload of the SSE `event: feed`: [updatedAt] is the feed's last content change (same clock as [YouTubeFeed.changedAt]). */
@Serializable
data class FeedSignal(
    @SerialName("updated_at") val updatedAt: Long = 0,
    @SerialName("new_count") val newCount: Int = 0,
    @SerialName("removed_count") val removedCount: Int = 0,
)

@Serializable
data class YouTubeVideo(
    val id: String,
    val url: String,
    val title: String = "",
    val channel: String = "",
    @SerialName("channel_id") val channelId: String? = null,
    val published: String? = null,
    val thumbnail: String? = null,
    @SerialName("duration_s") val durationS: Double? = null,
    @SerialName("is_short") val isShort: Boolean = false,
)

@Serializable
data class JfItem(
    val id: String,
    val name: String = "",
    val type: String = "",
    @SerialName("series_name") val seriesName: String? = null,
    val season: Int? = null,
    val episode: Int? = null,
    val year: Int? = null,
    val overview: String? = null,
    @SerialName("runtime_s") val runtimeS: Double? = null,
    val played: Boolean = false,
    @SerialName("played_pct") val playedPct: Double? = null,
    @SerialName("resume_s") val resumeS: Double? = null,
    @SerialName("image_url") val imageUrl: String? = null,
    @SerialName("backdrop_url") val backdropUrl: String? = null,
    @SerialName("child_count") val childCount: Int? = null,
    val children: List<JfItem>? = null,
) {
    val isPlayable: Boolean get() = type == "Movie" || type == "Episode" || type == "Video" || type == "MusicVideo"
    val isFolderLike: Boolean get() = type == "CollectionFolder" || type == "Folder" || type == "BoxSet" || type == "UserView"
    val canResume: Boolean get() = (resumeS ?: 0.0) > 5.0
    /** Progress in 0..1, if any. */
    val progress: Float?
        get() = playedPct?.let { (it / 100.0).toFloat().coerceIn(0f, 1f) }
            ?: if (resumeS != null && runtimeS != null && runtimeS > 0) (resumeS / runtimeS).toFloat().coerceIn(0f, 1f) else null

    val episodeLabel: String?
        get() = when {
            season != null && episode != null -> "S${season}E$episode"
            episode != null -> "E$episode"
            else -> null
        }
}

@Serializable
data class JfList(val items: List<JfItem> = emptyList(), val total: Int? = null)

@Serializable
data class CastRequest(val url: String)

@Serializable
@OptIn(ExperimentalSerializationApi::class)
data class ControlRequest(val action: String, @EncodeDefault(EncodeDefault.Mode.NEVER) val value: Double? = null)

@Serializable
data class ScreensaverRequest(val theme: String?)

@Serializable
data class PowerRequest(val action: String)

@Serializable
@OptIn(ExperimentalSerializationApi::class)
data class JfPlayRequest(
    val id: String,
    val resume: Boolean,
    @SerialName("low_quality") @EncodeDefault(EncodeDefault.Mode.NEVER) val lowQuality: Boolean? = null,
)

/** Human label for State.source. */
fun sourceLabel(source: String?): String = when (source) {
    "youtube" -> "YouTube"
    "jellyfin" -> "mrpflix"
    "url" -> "Web"
    "screensaver" -> "Screensaver"
    else -> "Projector"
}
