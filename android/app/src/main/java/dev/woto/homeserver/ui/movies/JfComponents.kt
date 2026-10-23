package dev.woto.homeserver.ui.movies

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.Movie
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil3.compose.SubcomposeAsyncImage
import dev.woto.homeserver.data.JfItem
import dev.woto.homeserver.ui.components.rememberImageUrl

fun JfItem.subtitleLine(): String? = when (type) {
    "Episode" -> listOfNotNull(seriesName, episodeLabel).joinToString(" · ").ifEmpty { null }
    "Series" -> listOfNotNull(year?.toString(), childCount?.let { "$it seasons" }).joinToString(" · ").ifEmpty { null }
    else -> year?.toString()
}

@Composable
fun JfImage(path: String?, modifier: Modifier = Modifier, contentScale: ContentScale = ContentScale.Crop) {
    val url = rememberImageUrl(path)
    Box(modifier.background(MaterialTheme.colorScheme.surfaceContainerHigh), contentAlignment = Alignment.Center) {
        if (url != null) {
            SubcomposeAsyncImage(
                model = url, contentDescription = null, contentScale = contentScale, modifier = Modifier.fillMaxSize(),
                error = { Icon(Icons.Rounded.Movie, null, tint = MaterialTheme.colorScheme.onSurfaceVariant) },
            )
        } else {
            Icon(Icons.Rounded.Movie, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
fun WatchedBadge(modifier: Modifier = Modifier) {
    Surface(modifier.size(24.dp), shape = CircleShape, color = MaterialTheme.colorScheme.primary) {
        Icon(Icons.Rounded.Check, "Watched", Modifier.padding(4.dp), tint = MaterialTheme.colorScheme.onPrimary)
    }
}

@Composable
fun ProgressOverlay(progress: Float?, modifier: Modifier = Modifier) {
    if (progress == null || progress <= 0f) return
    LinearProgressIndicator(
        progress = { progress },
        modifier = modifier.fillMaxWidth().height(4.dp),
        trackColor = Color.Black.copy(alpha = 0.5f),
        drawStopIndicator = {},
        gapSize = 0.dp,
    )
}

/** Portrait poster card (movies, series). */
@Composable
fun PosterCard(item: JfItem, onClick: () -> Unit, width: Dp? = 132.dp, modifier: Modifier = Modifier) {
    Column((if (width != null) modifier.width(width) else modifier.fillMaxWidth()).clip(RoundedCornerShape(14.dp)).clickable(onClick = onClick)) {
        Box(Modifier.fillMaxWidth().aspectRatio(2f / 3f).clip(RoundedCornerShape(14.dp))) {
            JfImage(item.imageUrl, Modifier.fillMaxSize())
            if (item.played) WatchedBadge(Modifier.align(Alignment.TopEnd).padding(6.dp))
            ProgressOverlay(item.progress?.takeIf { !item.played }, Modifier.align(Alignment.BottomCenter))
        }
        Text(item.name, style = MaterialTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 6.dp, start = 2.dp))
        item.subtitleLine()?.let {
            Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(start = 2.dp))
        }
    }
}

/** Landscape card (continue watching / next up / episodes). */
@Composable
fun WideCard(item: JfItem, onClick: () -> Unit, width: Dp = 260.dp) {
    Column(Modifier.width(width).clip(RoundedCornerShape(14.dp)).clickable(onClick = onClick)) {
        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(14.dp))) {
            JfImage(item.backdropUrl ?: item.imageUrl, Modifier.fillMaxSize())
            if (item.played) WatchedBadge(Modifier.align(Alignment.TopEnd).padding(6.dp))
            ProgressOverlay(item.progress?.takeIf { !item.played }, Modifier.align(Alignment.BottomCenter))
        }
        Text(
            if (item.type == "Episode") item.seriesName ?: item.name else item.name,
            style = MaterialTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 6.dp, start = 2.dp),
        )
        Text(
            if (item.type == "Episode") listOfNotNull(item.episodeLabel, item.name).joinToString(" · ") else item.subtitleLine().orEmpty(),
            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(start = 2.dp),
        )
    }
}
