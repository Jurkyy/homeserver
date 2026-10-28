package dev.woto.homeserver.playback

import android.os.Looper
import androidx.core.net.toUri
import androidx.media3.common.C
import androidx.media3.common.DeviceInfo
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.SimpleBasePlayer
import androidx.media3.common.util.UnstableApi
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import dev.woto.homeserver.data.MediaTarget
import dev.woto.homeserver.data.RemoteActions
import dev.woto.homeserver.data.SpotifyState
import dev.woto.homeserver.data.StateSnapshot
import dev.woto.homeserver.data.Urls
import dev.woto.homeserver.data.sourceLabel
import kotlin.math.roundToInt

/**
 * A media3 [Player] that doesn't play anything locally: it mirrors the projector's state and forwards
 * transport commands to the server. Exposed through a MediaSession for the notification / lock screen.
 */
@UnstableApi
class RemotePlayer(
    looper: Looper,
    private val snapshot: () -> StateSnapshot?,
    private val baseUrl: () -> String?,
    private val actions: RemoteActions,
) : SimpleBasePlayer(looper) {

    private val remoteDevice = DeviceInfo.Builder(DeviceInfo.PLAYBACK_TYPE_REMOTE)
        .setMinVolume(0)
        .setMaxVolume(VOLUME_STEPS)
        .build()

    private val castCommands = Player.Commands.Builder()
        .addAll(
            COMMAND_PLAY_PAUSE,
            COMMAND_STOP,
            COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM,
            COMMAND_SEEK_BACK,
            COMMAND_SEEK_FORWARD,
            COMMAND_GET_CURRENT_MEDIA_ITEM,
            COMMAND_GET_METADATA,
            COMMAND_GET_TIMELINE,
            COMMAND_GET_DEVICE_VOLUME,
            COMMAND_SET_DEVICE_VOLUME_WITH_FLAGS,
            COMMAND_ADJUST_DEVICE_VOLUME_WITH_FLAGS,
        )
        .build()

    private val spotifyCommands = Player.Commands.Builder()
        .addAll(
            COMMAND_PLAY_PAUSE,
            COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM,
            COMMAND_SEEK_BACK,
            COMMAND_SEEK_FORWARD,
            COMMAND_SEEK_TO_PREVIOUS,
            COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
            COMMAND_SEEK_TO_NEXT,
            COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
            COMMAND_GET_CURRENT_MEDIA_ITEM,
            COMMAND_GET_METADATA,
            COMMAND_GET_TIMELINE,
        )
        .build()

    private val spotifyVolumeCommands = spotifyCommands.buildUpon()
        .addAll(COMMAND_GET_DEVICE_VOLUME, COMMAND_SET_DEVICE_VOLUME_WITH_FLAGS, COMMAND_ADJUST_DEVICE_VOLUME_WITH_FLAGS)
        .build()

    /** Which backend the transport commands currently go to; captured with each state so handlers stay consistent. */
    private var target: MediaTarget = MediaTarget.None

    override fun getState(): State {
        val snap = snapshot()
        val s = snap?.state
        target = s?.mediaTarget ?: MediaTarget.None
        val builder = State.Builder()
            .setDeviceInfo(remoteDevice)
            .setSeekBackIncrementMs(10_000)
            .setSeekForwardIncrementMs(30_000)
        return when (target) {
            MediaTarget.Cast -> castState(builder, snap!!)
            MediaTarget.Spotify -> spotifyState(builder, s!!.controllableSpotify!!)
            MediaTarget.None -> builder.setAvailableCommands(castCommands)
                .setPlaybackState(STATE_IDLE).setPlayWhenReady(false, PLAY_WHEN_READY_CHANGE_REASON_REMOTE).build()
        }
    }

    private fun castState(builder: State.Builder, snap: StateSnapshot): State {
        val s = snap.state
        val metadata = MediaMetadata.Builder()
            .setTitle(s.title ?: "Playing on projector")
            .setArtist(s.subtitle ?: sourceLabel(s.source))
            .setDisplayTitle(s.title)
            .setArtworkUri(Urls.resolve(baseUrl(), s.artworkUrl)?.toUri())
            .setIsPlayable(true)
            .setIsBrowsable(false)
            .build()
        val id = s.item?.id ?: (s.title ?: "current")
        val item = mediaItem(id, metadata, s.durationS)
        val posMs = ((snap.positionAt() ?: 0.0) * 1000).toLong()
        return builder
            .setAvailableCommands(castCommands)
            .setPlaylist(listOf(item))
            .setCurrentMediaItemIndex(0)
            .setPlaybackState(STATE_READY)
            .setPlayWhenReady(s.isPlaying, PLAY_WHEN_READY_CHANGE_REASON_REMOTE)
            .setContentPositionMs(
                if (s.isPlaying) PositionSupplier.getExtrapolating(posMs, 1f) else PositionSupplier.getConstant(posMs),
            )
            .setDeviceVolume(toSteps(s.volume))
            .setIsDeviceMuted(s.muted)
            .build()
    }

    private fun spotifyState(builder: State.Builder, sp: SpotifyState): State {
        val metadata = MediaMetadata.Builder()
            .setTitle(sp.title ?: "Spotify")
            .setArtist(sp.artist ?: "Spotify")
            .setAlbumTitle(sp.album)
            .setDisplayTitle(sp.title)
            .setArtworkUri(Urls.resolve(baseUrl(), sp.artworkUrl)?.toUri())
            .setIsPlayable(true)
            .setIsBrowsable(false)
            .build()
        val id = "spotify:" + listOfNotNull(sp.title, sp.artist).joinToString("|")
        // A placeholder "next" item makes media3 route next (seekToNext) into handleSeek; it never becomes current.
        val next = MediaItemData.Builder(SPOTIFY_NEXT_UID)
            .setMediaItem(MediaItem.Builder().setMediaId(SPOTIFY_NEXT_UID).build())
            .build()
        val posMs = ((sp.positionAt() ?: 0.0) * 1000).toLong()
        val volume = sp.volume
        builder
            .setAvailableCommands(if (volume != null) spotifyVolumeCommands else spotifyCommands)
            .setPlaylist(listOf(mediaItem(id, metadata, sp.durationS), next))
            .setCurrentMediaItemIndex(0)
            .setPlaybackState(STATE_READY)
            .setPlayWhenReady(sp.playing, PLAY_WHEN_READY_CHANGE_REASON_REMOTE)
            .setContentPositionMs(
                if (sp.playing) PositionSupplier.getExtrapolating(posMs, 1f) else PositionSupplier.getConstant(posMs),
            )
        if (volume != null) builder.setDeviceVolume(toSteps(volume)).setIsDeviceMuted(false)
        return builder.build()
    }

    private fun mediaItem(id: String, metadata: MediaMetadata, durationS: Double?): MediaItemData {
        val durationUs = durationS?.takeIf { it > 0 }?.let { (it * 1_000_000).toLong() } ?: C.TIME_UNSET
        return MediaItemData.Builder(id)
            .setMediaItem(MediaItem.Builder().setMediaId(id).setMediaMetadata(metadata).build())
            .setDurationUs(durationUs)
            .setIsSeekable(durationS != null)
            .setIsDynamic(false)
            .build()
    }

    private fun toSteps(volume: Double) = (volume / 100.0 * VOLUME_STEPS).roundToInt().coerceIn(0, VOLUME_STEPS)

    fun refresh() = invalidateState()

    private val spotify: Boolean get() = target == MediaTarget.Spotify

    override fun handleSetPlayWhenReady(playWhenReady: Boolean): ListenableFuture<*> {
        when {
            spotify && playWhenReady -> actions.spotifyPlay()
            spotify -> actions.spotifyPause()
            playWhenReady -> actions.play()
            else -> actions.pause()
        }
        return Futures.immediateVoidFuture()
    }

    override fun handleStop(): ListenableFuture<*> {
        if (spotify) actions.spotifyPause() else actions.stop()
        return Futures.immediateVoidFuture()
    }

    override fun handleSeek(mediaItemIndex: Int, positionMs: Long, seekCommand: Int): ListenableFuture<*> {
        val seconds = positionMs.coerceAtLeast(0) / 1000.0
        if (!spotify) {
            actions.seekTo(seconds)
        } else {
            when (spotifySeekAction(seekCommand, mediaItemIndex)) {
                SpotifySeek.Next -> actions.spotifyNext()
                SpotifySeek.Previous -> actions.spotifyPrevious()
                SpotifySeek.SeekTo -> actions.spotifySeekTo(seconds)
            }
        }
        return Futures.immediateVoidFuture()
    }

    override fun handleSetDeviceVolume(deviceVolume: Int, flags: Int): ListenableFuture<*> {
        val v = deviceVolume * 100.0 / VOLUME_STEPS
        if (spotify) actions.spotifySetVolume(v) else actions.setVolume(v)
        return Futures.immediateVoidFuture()
    }

    override fun handleIncreaseDeviceVolume(flags: Int): ListenableFuture<*> {
        if (spotify) actions.spotifyVolumeDelta(100.0 / VOLUME_STEPS) else actions.volumeDelta(100.0 / VOLUME_STEPS)
        return Futures.immediateVoidFuture()
    }

    override fun handleDecreaseDeviceVolume(flags: Int): ListenableFuture<*> {
        if (spotify) actions.spotifyVolumeDelta(-100.0 / VOLUME_STEPS) else actions.volumeDelta(-100.0 / VOLUME_STEPS)
        return Futures.immediateVoidFuture()
    }

    override fun handleRelease(): ListenableFuture<*> = Futures.immediateVoidFuture()

    enum class SpotifySeek { Next, Previous, SeekTo }

    companion object {
        const val VOLUME_STEPS = 20 // 5 % per hardware key press
        private const val SPOTIFY_NEXT_UID = "spotify:next"

        /**
         * Maps a media3 seek onto a Spotify action. Next/previous arrive as seeks (to the placeholder next item, or
         * to the start of the current one for "previous"); everything else is a position seek within the track.
         */
        fun spotifySeekAction(seekCommand: Int, mediaItemIndex: Int): SpotifySeek = when {
            seekCommand == COMMAND_SEEK_TO_NEXT || seekCommand == COMMAND_SEEK_TO_NEXT_MEDIA_ITEM -> SpotifySeek.Next
            seekCommand == COMMAND_SEEK_TO_PREVIOUS || seekCommand == COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM -> SpotifySeek.Previous
            mediaItemIndex > 0 -> SpotifySeek.Next // e.g. a controller jumping to the placeholder item directly
            else -> SpotifySeek.SeekTo
        }
    }
}
