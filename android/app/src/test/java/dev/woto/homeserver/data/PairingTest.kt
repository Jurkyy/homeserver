package dev.woto.homeserver.data

import dev.woto.homeserver.share.ShareParsing
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingTest {

    @Test
    fun parsesEncodedPairingUri() {
        val info = Pairing.parse("mediacast://pair?url=http%3A%2F%2Fhomeserver.local&token=abc123")
        assertEquals(PairingInfo("http://homeserver.local", "abc123"), info)
    }

    @Test
    fun parsesIpWithPortAndTrailingSlash() {
        val info = Pairing.parse("mediacast://pair?token=t0k%2Ben&url=http%3A%2F%2F192.168.0.22%3A8765%2F")
        assertEquals(PairingInfo("http://192.168.0.22:8765", "t0k+en"), info)
    }

    @Test
    fun acceptsUnencodedUrlAndApiSuffix() {
        val info = Pairing.parse("mediacast://pair?url=http://homeserver.local/api/v1&token=x")
        assertEquals("http://homeserver.local", info?.baseUrl)
    }

    @Test
    fun rejectsInvalid() {
        assertNull(Pairing.parse(null))
        assertNull(Pairing.parse(""))
        assertNull(Pairing.parse("https://example.com/?url=x&token=y"))
        assertNull(Pairing.parse("mediacast://other?url=http%3A%2F%2Fa&token=y"))
        assertNull(Pairing.parse("mediacast://pair?url=http%3A%2F%2Fa"))
        assertNull(Pairing.parse("mediacast://pair?token=abc"))
        assertNull(Pairing.parse("mediacast://pair?url=ftp%3A%2F%2Fa&token=abc"))
    }

    @Test
    fun normalizesBaseUrls() {
        assertEquals("http://homeserver.local", Pairing.normalizeBaseUrl("homeserver.local"))
        assertEquals("https://home", Pairing.normalizeBaseUrl(" https://home/ "))
        assertNull(Pairing.normalizeBaseUrl(""))
        assertNull(Pairing.normalizeBaseUrl("mailto:x"))
    }

    @Test
    fun resolvesRelativeUrls() {
        assertEquals("http://homeserver.local/api/v1/artwork/a.jpg", Urls.resolve("http://homeserver.local", "/api/v1/artwork/a.jpg"))
        assertEquals("http://192.168.0.22:8765/api/v1/x", Urls.resolve("http://192.168.0.22:8765/", "/api/v1/x"))
        assertEquals("https://i.ytimg.com/a.jpg", Urls.resolve("http://homeserver.local", "https://i.ytimg.com/a.jpg"))
        assertEquals("http://h/sub/api/v1/x", Urls.resolve("http://h/sub", "/api/v1/x"))
        assertNull(Urls.resolve("http://h", null))
        assertNull(Urls.resolve(null, "/x"))
    }

    @Test
    fun extractsSharedUrl() {
        assertEquals("https://youtu.be/abc?si=x", ShareParsing.extractUrl("Check this https://youtu.be/abc?si=x"))
        assertEquals("https://www.youtube.com/watch?v=abc", ShareParsing.extractUrl("https://www.youtube.com/watch?v=abc."))
        assertNull(ShareParsing.extractUrl("no link here"))
        assertNull(ShareParsing.extractUrl(null))
    }
}
