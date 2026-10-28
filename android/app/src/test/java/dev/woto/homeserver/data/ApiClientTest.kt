package dev.woto.homeserver.data

import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class ApiClientTest {
    private lateinit var server: MockWebServer
    private lateinit var api: ApiClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        val base = server.url("/").toString().trimEnd('/')
        api = ApiClient(OkHttpClient()) { Credentials(base, "secret") }
    }

    @After
    fun tearDown() = server.shutdown()

    @Test
    fun sendsBearerAndParsesState() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"power":"on","activity":"idle","volume":40}"""))
        val s = api.state()
        assertEquals(40.0, s.volume, 0.0)
        val req = server.takeRequest()
        assertEquals("/api/v1/state", req.path)
        assertEquals("Bearer secret", req.getHeader("Authorization"))
    }

    @Test
    fun postsControlJson() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        api.control("seek_to", 120.0)
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/api/v1/control", req.path)
        assertEquals("""{"action":"seek_to","value":120.0}""", req.body.readUtf8())
    }

    @Test
    fun postsSpotifyControlJson() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        api.spotifyControl("volume", 40.0)
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        api.spotifyControl("next")
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/api/v1/spotify/control", req.path)
        assertEquals("Bearer secret", req.getHeader("Authorization"))
        assertEquals("""{"action":"volume","value":40.0}""", req.body.readUtf8())
        assertEquals("""{"action":"next"}""", server.takeRequest().body.readUtf8())
    }

    @Test
    fun surfacesSpotifyErrors() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(409).setBody("""{"error":"Spotify isn't playing on Homeserver","code":"spotify_error"}"""))
        try {
            api.spotifyControl("toggle")
            fail("expected exception")
        } catch (e: ApiException) {
            assertEquals(409, e.status)
            assertEquals("spotify_error", e.code)
        }
    }

    @Test
    fun buildsQueryParams() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"items":[],"total":0}"""))
        api.jfItems(parentId = "p 1", search = null, start = 60, limit = 60)
        val path = server.takeRequest().path!!
        assertTrue(path, path.startsWith("/api/v1/jellyfin/items?"))
        assertTrue(path, path.contains("parent_id=p%201"))
        assertTrue(path, !path.contains("search="))
        assertTrue(path, path.contains("start=60"))
    }

    @Test
    fun surfacesServerErrorMessage() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":"invalid token","code":"unauthorized"}"""))
        try {
            api.state()
            fail("expected exception")
        } catch (e: ApiException) {
            assertEquals(401, e.status)
            assertEquals("invalid token", e.message)
            assertEquals("unauthorized", e.code)
        }
    }

    @Test
    fun healthIsUnauthenticated() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"ok":true,"version":"1.2.3"}"""))
        val h = api.health(server.url("/").toString())
        assertEquals("1.2.3", h.version)
        assertEquals(null, server.takeRequest().getHeader("Authorization"))
    }
}
