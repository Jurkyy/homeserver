package dev.woto.homeserver.playback

import android.os.Looper
import androidx.core.net.toUri
import androidx.media3.common.DeviceInfo
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.SimpleBasePlayer
import androidx.media3.common.util.UnstableApi
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import dev.woto.homeserver.data.RemoteActions
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

    private val commands = Player.Commands.Builder()
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

    override fun getState(): State {
        val snap = snapshot()
        val s = snap?.state
        val builder = State.Builder()
            .setAvailableCommands(commands)
            .setDeviceInfo(remoteDevice)
            .setSeekBackIncrementMs(10_000)
            .setSeekForwardIncrementMs(30_000)
        if (s == null || !s.hasMedia || s.isSleeping) {
            return builder.setPlaybackState(STATE_IDLE).setPlayWhenReady(false, PLAY_WHEN_READY_CHANGE_REASON_REMOTE).build()
        }
        val metadata = MediaMetadata.Builder()
            .setTitle(s.title ?: "Playing on projector")
            .setArtist(s.subtitle ?: sourceLabel(s.source))
            .setDisplayTitle(s.title)
            .setArtworkUri(Urls.resolve(baseUrl(), s.artworkUrl)?.toUri())
            .setIsPlayable(true)
            .setIsBrowsable(false)
            .build()
        val id = s.item?.id ?: (s.title ?: "current")
        val durationUs = s.durationS?.takeIf { it > 0 }?.let { (it * 1_000_000).toLong() } ?: androidx.media3.common.C.TIME_UNSET
        val item = MediaItemData.Builder(id)
            .setMediaItem(MediaItem.Builder().setMediaId(id).setMediaMetadata(metadata).build())
            .setDurationUs(durationUs)
            .setIsSeekable(s.durationS != null)
            .setIsDynamic(false)
            .build()
        val posMs = ((snap.positionAt() ?: 0.0) * 1000).toLong()
        return builder
            .setPlaylist(listOf(item))
            .setCurrentMediaItemIndex(0)
            .setPlaybackState(STATE_READY)
            .setPlayWhenReady(s.isPlaying, PLAY_WHEN_READY_CHANGE_REASON_REMOTE)
            .setContentPositionMs(
                if (s.isPlaying) PositionSupplier.getExtrapolating(posMs, 1f) else PositionSupplier.getConstant(posMs),
            )
            .setDeviceVolume((s.volume / 100.0 * VOLUME_STEPS).roundToInt().coerceIn(0, VOLUME_STEPS))
            .setIsDeviceMuted(s.muted)
            .build()
    }

    fun refresh() = invalidateState()

    override fun handleSetPlayWhenReady(playWhenReady: Boolean): ListenableFuture<*> {
        if (playWhenReady) actions.play() else actions.pause()
        return Futures.immediateVoidFuture()
    }

    override fun handleStop(): ListenableFuture<*> {
        actions.stop()
        return Futures.immediateVoidFuture()
    }

    override fun handleSeek(mediaItemIndex: Int, positionMs: Long, seekCommand: Int): ListenableFuture<*> {
        actions.seekTo(positionMs.coerceAtLeast(0) / 1000.0)
        return Futures.immediateVoidFuture()
    }

    override fun handleSetDeviceVolume(deviceVolume: Int, flags: Int): ListenableFuture<*> {
        actions.setVolume(deviceVolume * 100.0 / VOLUME_STEPS)
        return Futures.immediateVoidFuture()
    }

    override fun handleIncreaseDeviceVolume(flags: Int): ListenableFuture<*> {
        actions.volumeDelta(100.0 / VOLUME_STEPS)
        return Futures.immediateVoidFuture()
    }

    override fun handleDecreaseDeviceVolume(flags: Int): ListenableFuture<*> {
        actions.volumeDelta(-100.0 / VOLUME_STEPS)
        return Futures.immediateVoidFuture()
    }

    override fun handleRelease(): ListenableFuture<*> = Futures.immediateVoidFuture()

    companion object {
        const val VOLUME_STEPS = 20 // 5 % per hardware key press
    }
}
