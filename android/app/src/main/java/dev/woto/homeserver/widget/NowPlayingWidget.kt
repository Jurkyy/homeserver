package dev.woto.homeserver.widget

import android.content.Context
import android.graphics.Bitmap
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.ColorFilter
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.action.ActionParameters
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.action.actionStartActivity
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.updateAll
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import coil3.SingletonImageLoader
import coil3.request.ImageRequest
import coil3.request.allowHardware
import coil3.toBitmap
import dev.woto.homeserver.MainActivity
import dev.woto.homeserver.R
import dev.woto.homeserver.data.MediaTarget
import dev.woto.homeserver.data.ServerState
import dev.woto.homeserver.data.Urls
import dev.woto.homeserver.graph
import dev.woto.homeserver.data.sourceLabel

class NowPlayingWidget : GlanceAppWidget() {

    /** The fields the widget shows; used to avoid needless widget updates. */
    data class Key(
        val title: String?, val subtitle: String?, val activity: String, val power: String, val artwork: String?,
        val target: MediaTarget, val spotifyTitle: String?, val spotifyArtist: String?, val spotifyPlaying: Boolean, val spotifyArtwork: String?,
    ) {
        companion object {
            fun of(s: ServerState): Key {
                val sp = s.controllableSpotify
                return Key(s.title, s.subtitle, s.activity, s.power, s.artworkUrl, s.mediaTarget, sp?.title, sp?.artist, sp?.playing == true, sp?.artworkUrl)
            }
        }
    }

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val g = context.graph
        val paired = g.settings.current.isPaired
        val state = g.repository.state ?: if (paired) g.repository.refresh().getOrNull() else null
        val artwork = when (state?.mediaTarget) {
            MediaTarget.Cast -> state.artworkUrl
            MediaTarget.Spotify -> state.controllableSpotify?.artworkUrl
            else -> null
        }
        val art = artwork?.let { loadBitmap(context, Urls.resolve(g.settings.current.baseUrl, it)) }
        provideContent {
            GlanceTheme {
                Content(state, paired, art)
            }
        }
    }

    private suspend fun loadBitmap(context: Context, url: String?): Bitmap? {
        if (url == null) return null
        return runCatching {
            val req = ImageRequest.Builder(context).data(url).size(256).allowHardware(false).build()
            SingletonImageLoader.get(context).execute(req).image?.toBitmap()
        }.getOrNull()
    }

    @Composable
    private fun Content(state: ServerState?, paired: Boolean, art: Bitmap?) {
        val target = state?.mediaTarget ?: MediaTarget.None
        val spotify = state?.controllableSpotify
        val title = when {
            !paired -> "Not paired"
            state == null -> "Projector offline"
            target == MediaTarget.Cast -> state.title ?: "Playing"
            target == MediaTarget.Spotify -> spotify?.title ?: "Spotify"
            state.isSleeping -> "Projector sleeping"
            state.activity == "screensaver" -> "Screensaver"
            else -> "Nothing playing"
        }
        val subtitle = when (target) {
            MediaTarget.Cast -> state?.subtitle ?: sourceLabel(state?.source)
            MediaTarget.Spotify -> listOfNotNull("Spotify", spotify?.artist).joinToString(" · ")
            MediaTarget.None -> "Homeserver"
        }
        val playing = if (target == MediaTarget.Spotify) spotify?.playing == true else state?.isPlaying == true
        Row(
            modifier = GlanceModifier.fillMaxSize().background(GlanceTheme.colors.widgetBackground).cornerRadius(24.dp).padding(12.dp)
                .clickable(actionStartActivity<MainActivity>()),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(GlanceModifier.size(56.dp).cornerRadius(12.dp).background(GlanceTheme.colors.secondaryContainer), contentAlignment = Alignment.Center) {
                if (art != null) {
                    Image(ImageProvider(art), contentDescription = null, modifier = GlanceModifier.size(56.dp).cornerRadius(12.dp))
                } else {
                    Image(ImageProvider(R.drawable.ic_stat_projector), contentDescription = null, modifier = GlanceModifier.size(28.dp),
                        colorFilter = ColorFilter.tint(GlanceTheme.colors.onSecondaryContainer))
                }
            }
            Spacer(GlanceModifier.width(12.dp))
            Column(GlanceModifier.defaultWeight()) {
                Text(title, maxLines = 1, style = TextStyle(color = GlanceTheme.colors.onSurface, fontWeight = FontWeight.Medium, fontSize = 15.sp))
                Text(subtitle, maxLines = 1, style = TextStyle(color = GlanceTheme.colors.onSurfaceVariant, fontSize = 13.sp))
            }
            if (target != MediaTarget.None) {
                Spacer(GlanceModifier.width(8.dp))
                Box(
                    GlanceModifier.size(48.dp).cornerRadius(24.dp).background(GlanceTheme.colors.primary).clickable(actionRunCallback<TogglePlayAction>()),
                    contentAlignment = Alignment.Center,
                ) {
                    Image(
                        ImageProvider(if (playing) R.drawable.ic_pause else R.drawable.ic_play),
                        contentDescription = if (playing) "Pause" else "Play",
                        modifier = GlanceModifier.size(24.dp),
                        colorFilter = ColorFilter.tint(GlanceTheme.colors.onPrimary),
                    )
                }
            }
        }
    }

    companion object {
        @Suppress("UNUSED_PARAMETER")
        suspend fun updateAll(context: Context, key: Key) {
            runCatching { NowPlayingWidget().updateAll(context) }
        }
    }
}

class TogglePlayAction : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        val g = context.graph
        // Same routing as the media notification: the cast player wins, else a controllable Spotify session.
        val state = g.repository.state ?: g.repository.refresh().getOrNull()
        runCatching {
            if (state?.mediaTarget == MediaTarget.Spotify) g.api.spotifyControl("toggle") else g.api.control("toggle")
        }
        g.repository.refresh()
        NowPlayingWidget().updateAll(context)
    }
}

class NowPlayingWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = NowPlayingWidget()
}

