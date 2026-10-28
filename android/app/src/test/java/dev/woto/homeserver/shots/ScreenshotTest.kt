package dev.woto.homeserver.shots

import android.graphics.Bitmap
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.activity.ComponentActivity
import dev.woto.homeserver.data.*
import dev.woto.homeserver.graph
import dev.woto.homeserver.ui.remote.RemoteScreen
import dev.woto.homeserver.ui.pairing.PairingScreen
import dev.woto.homeserver.ui.theme.HomeserverTheme
import kotlinx.coroutines.runBlocking
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.io.FileOutputStream

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [35], qualifiers = "w411dp-h891dp-xxhdpi")
/** Renders key screens to PNG under Robolectric (build/shots, or $SHOTS_DIR) for visual review. */
class ScreenshotTest {
    @get:Rule val rule = createAndroidComposeRule<ComponentActivity>()

    private fun save(name: String) {
        val v = rule.activity.window.decorView
        val bmp = Bitmap.createBitmap(v.width, v.height, Bitmap.Config.ARGB_8888)
        rule.runOnUiThread { v.draw(android.graphics.Canvas(bmp)) }
        val dir = File(System.getenv("SHOTS_DIR") ?: "build/shots").apply { mkdirs() }
        FileOutputStream(File(dir, "$name.png")).use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    private val playing = ServerState(
        power = "on", activity = "playing", source = "youtube", title = "Every PHP server from the last decade just got owned",
        subtitle = "Fireship", positionS = 75.0, durationS = 312.0, volume = 60.0,
        screensaver = ScreensaverState(themes = listOf(ScreensaverTheme("woods", "🌲 Woods"), ScreensaverTheme("waterfall", "💧 Waterfall"), ScreensaverTheme("fire", "🔥 Fire")), idleMinutes = 10.0),
        spotify = SpotifyState(active = true, playing = true, title = "Midnight City", artist = "M83", album = "Hurry Up", positionS = 60.0, durationS = 240.0, updatedAt = System.currentTimeMillis()),
    )

    @Test fun remotePlaying() {
        val g = rule.activity.graph
        g.repository.push(playing)
        rule.setContent { HomeserverTheme(dynamicColor = false) { RemoteScreen(g) {} } }
        rule.waitForIdle()
        save("remote_playing")
    }

    @Test fun remoteSpotify() {
        val g = rule.activity.graph
        g.repository.push(
            playing.copy(
                activity = "idle", source = null, title = null, subtitle = null, positionS = null, durationS = null,
                spotify = playing.spotify!!.copy(controllable = true, volume = 45.0),
            ),
        )
        rule.setContent { HomeserverTheme(dynamicColor = false) { RemoteScreen(g) {} } }
        rule.waitForIdle()
        save("remote_spotify")
    }

    @Test fun remoteSleeping() {
        val g = rule.activity.graph
        g.repository.push(playing.copy(power = "sleeping", activity = "idle", spotify = null))
        rule.setContent { HomeserverTheme(dynamicColor = false) { RemoteScreen(g) {} } }
        rule.waitForIdle()
        save("remote_sleeping")
    }

    @Test fun pairing() {
        val g = rule.activity.graph
        rule.setContent { HomeserverTheme(dynamicColor = false) { PairingScreen(g, null, {}, {}, false, {}, {}) } }
        rule.waitForIdle()
        save("pairing")
    }
}
