package dev.woto.homeserver.data

import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Read-only smoke test against a real server. Skipped unless MEDIACAST_URL and MEDIACAST_TOKEN are set:
 * `MEDIACAST_URL=http://192.168.0.22 MEDIACAST_TOKEN=… ./gradlew testDebugUnitTest --tests '*LiveSmokeTest*'`
 */
class LiveSmokeTest {
    private val url = System.getenv("MEDIACAST_URL")
    private val token = System.getenv("MEDIACAST_TOKEN")

    private fun api(): ApiClient {
        assumeTrue("MEDIACAST_URL/MEDIACAST_TOKEN not set", !url.isNullOrBlank() && !token.isNullOrBlank())
        return ApiClient(ApiClient.defaultHttp().newBuilder().readTimeout(60, TimeUnit.SECONDS).build()) { Credentials(url!!, token!!) }
    }

    @Test
    fun readEndpointsParse() = runBlocking {
        val api = api()
        assertTrue(api.health(url!!).ok)
        val state = api.state()
        println("state: power=${state.power} activity=${state.activity} themes=${state.screensaver?.themes?.size} spotify=${state.spotify != null}")
        val feed = api.youtubeFeed(refresh = false)
        println("feed: ${feed.items.size} items, shorts=${feed.items.count { it.isShort }}")
        val views = api.jfViews().items
        println("views: ${views.map { it.name to it.type }}")
        println("resume: ${api.jfResume().items.size}, nextup: ${api.jfNextUp().items.size}")
        val first = views.firstOrNull()
        if (first != null) {
            val items = api.jfItems(parentId = first.id, limit = 10)
            println("items in ${first.name}: ${items.items.size}/${items.total}")
            val series = api.jfItems(search = null, limit = 20, types = "Series").items.firstOrNull()
            if (series != null) {
                val detail = api.jfItem(series.id)
                println("series ${detail.name}: ${detail.children?.size} children (${detail.children?.firstOrNull()?.type})")
                detail.children?.firstOrNull()?.let { season ->
                    val s = api.jfItem(season.id)
                    println("season ${s.name}: ${s.children?.size} episodes, first=${s.children?.firstOrNull()?.let { it.episodeLabel + " " + it.name }}")
                }
            }
        }
        assertNotNull(Urls.resolve(url, views.firstOrNull()?.imageUrl ?: "/x"))
    }

    @Test
    fun sseDeliversInitialState() {
        val api = api()
        val latch = CountDownLatch(1)
        var got: ServerState? = null
        val req: Request = api.eventsRequest()!!
        val es = EventSources.createFactory(OkHttpClient()).newEventSource(req, object : EventSourceListener() {
            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                (SseEvents.decode(type, data) as? ServerEvent.State)?.let { got = it.state; latch.countDown() }
            }
        })
        latch.await(10, TimeUnit.SECONDS)
        es.cancel()
        assertNotNull("no state event within 10s", got)
        println("sse state: ${got?.activity}")
    }
}
