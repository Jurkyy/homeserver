package dev.woto.homeserver.playback

import androidx.annotation.OptIn
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import dev.woto.homeserver.playback.RemotePlayer.SpotifySeek
import org.junit.Assert.assertEquals
import org.junit.Test

@OptIn(UnstableApi::class)
class RemotePlayerTest {
    @Test
    fun mapsSpotifySeeksToActions() {
        assertEquals(SpotifySeek.Next, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_TO_NEXT, 1))
        assertEquals(SpotifySeek.Next, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM, 1))
        assertEquals(SpotifySeek.Previous, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_TO_PREVIOUS, 0))
        assertEquals(SpotifySeek.Previous, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM, 0))
        assertEquals(SpotifySeek.SeekTo, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM, 0))
        assertEquals(SpotifySeek.SeekTo, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_BACK, 0))
        assertEquals(SpotifySeek.SeekTo, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_FORWARD, 0))
        assertEquals(SpotifySeek.Next, RemotePlayer.spotifySeekAction(Player.COMMAND_SEEK_TO_MEDIA_ITEM, 1))
    }
}
