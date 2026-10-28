package dev.woto.homeserver.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import java.time.ZoneOffset
import org.junit.Test

class ModelParsingTest {

    private val fullState = """
        {
          "power": "on", "screen": "on", "activity": "playing", "source": "youtube",
          "title": "Some video", "subtitle": "Some channel", "artwork_url": "/api/v1/artwork/abc.jpg",
          "position_s": 12.5, "duration_s": 600, "volume": 70, "muted": false,
          "item": { "kind": "youtube", "id": "dQw4w9WgXcQ" },
          "screensaver": { "active": false, "theme": null, "themes": [{ "id": "aquarium", "name": "Aquarium" }, { "id": "fire", "name": "Fireplace" }], "idle_minutes": 10 },
          "spotify": { "active": true, "playing": true, "title": "Song", "artist": "Artist", "album": "Album",
                       "artwork_url": "https://i.scdn.co/image/x", "position_s": 30, "duration_s": 200, "updated_at": 1700000000000 },
          "updated_at": 1700000000123,
          "future_field": { "nested": [1, 2, 3] }
        }
    """.trimIndent()

    @Test
    fun parsesFullState() {
        val s = AppJson.decodeFromString(ServerState.serializer(), fullState)
        assertEquals("on", s.power)
        assertTrue(s.isPlaying)
        assertTrue(s.hasMedia)
        assertEquals("youtube", s.source)
        assertEquals(12.5, s.positionS!!, 0.0)
        assertEquals(600.0, s.durationS!!, 0.0)
        assertEquals(70.0, s.volume, 0.0)
        assertEquals("dQw4w9WgXcQ", s.item?.id)
        assertEquals(2, s.screensaver?.themes?.size)
        assertEquals("Fireplace", s.screensaver?.themes?.get(1)?.label)
        assertEquals(10.0, s.screensaver?.idleMinutes!!, 0.0)
        assertTrue(s.spotify!!.active)
        assertEquals("Artist", s.spotify?.artist)
        assertEquals(1700000000123, s.updatedAt)
    }

    @Test
    fun parsesMinimalIdleSleepingState() {
        val json = """{"power":"sleeping","screen":"off","activity":"idle","source":null,"title":null,"subtitle":null,
            "artwork_url":null,"position_s":null,"duration_s":null,"volume":0,"muted":true,"item":null,
            "screensaver":{"active":false,"theme":null,"themes":[],"idle_minutes":15},"spotify":null,"updated_at":1}"""
        val s = AppJson.decodeFromString(ServerState.serializer(), json)
        assertTrue(s.isSleeping)
        assertFalse(s.hasMedia)
        assertNull(s.spotify)
        assertNull(s.item)
        assertTrue(s.muted)
    }

    @Test
    fun toleratesMissingFields() {
        val s = AppJson.decodeFromString(ServerState.serializer(), """{"activity":"paused"}""")
        assertTrue(s.isPaused)
        assertEquals("on", s.power)
        assertNull(s.screensaver)
    }

    @Test
    fun parsesYouTubeFeed() {
        val json = """
            {"items":[
              {"id":"abc","url":"https://www.youtube.com/watch?v=abc","title":"Video A","channel":"Chan","channel_id":"UC1",
               "published":"2026-10-20T12:00:00+00:00","thumbnail":"https://i.ytimg.com/vi/abc/hqdefault.jpg","duration_s":754,"is_short":false},
              {"id":"def","url":"https://www.youtube.com/shorts/def","title":"Short","channel":"Chan","channel_id":"UC1",
               "published":"2026-10-21T08:30:00Z","thumbnail":"/api/v1/artwork/def.jpg","duration_s":null,"is_short":true},
              {"id":"ghi","url":"https://www.youtube.com/watch?v=ghi","title":"No duration","channel":"Other","channel_id":"UC2",
               "published":"2026-10-21T08:30:00Z","thumbnail":null,"is_short":false}
            ],"updated_at":1761040000000}
        """.trimIndent()
        val feed = AppJson.decodeFromString(YouTubeFeed.serializer(), json)
        assertEquals(3, feed.items.size)
        assertEquals(754.0, feed.items[0].durationS!!, 0.0)
        assertTrue(feed.items[1].isShort)
        assertNull(feed.items[1].durationS)
        assertNull(feed.items[2].durationS)
        assertEquals(1761040000000, feed.updatedAt)
    }

    @Test
    fun parsesJfItems() {
        val json = """
            {"items":[
              {"id":"m1","name":"Movie","type":"Movie","year":2010,"overview":"Dreams.","runtime_s":8880,"played":false,
               "played_pct":42.5,"resume_s":3774,"image_url":"/api/v1/jellyfin/image/m1?type=Primary","backdrop_url":"/api/v1/jellyfin/image/m1?type=Backdrop"},
              {"id":"e1","name":"Pilot","type":"Episode","series_name":"Show","season":1,"episode":3,"played":true,"image_url":"/api/v1/jellyfin/image/e1"},
              {"id":"s1","name":"Show","type":"Series","played":false,"image_url":"/x","child_count":4}
            ],"total":120}
        """.trimIndent()
        val list = AppJson.decodeFromString(JfList.serializer(), json)
        assertEquals(120, list.total)
        val movie = list.items[0]
        assertTrue(movie.isPlayable)
        assertTrue(movie.canResume)
        assertEquals(0.425f, movie.progress!!, 0.001f)
        val ep = list.items[1]
        assertEquals("S1E3", ep.episodeLabel)
        assertTrue(ep.played)
        assertFalse(list.items[2].isPlayable)
        assertEquals(4, list.items[2].childCount)
    }

    @Test
    fun parsesJfItemWithChildren() {
        val json = """{"id":"s1","name":"Show","type":"Series","played":false,"image_url":"/i",
            "children":[{"id":"se1","name":"Season 1","type":"Season","played":true,"image_url":"/i1","child_count":10}]}"""
        val item = AppJson.decodeFromString(JfItem.serializer(), json)
        assertNotNull(item.children)
        assertEquals("Season 1", item.children!![0].name)
        assertFalse(item.children!![0].isPlayable)
    }

    @Test
    fun progressFallsBackToResumeOverRuntime() {
        val item = JfItem(id = "x", type = "Movie", runtimeS = 100.0, resumeS = 25.0)
        assertEquals(0.25f, item.progress!!, 0.001f)
    }

    @Test
    fun parsesErrorBody() {
        val e = AppJson.decodeFromString(ErrorResponse.serializer(), """{"error":"bad token","code":"unauthorized"}""")
        assertEquals("bad token", e.error)
        assertEquals("unauthorized", e.code)
    }

    @Test
    fun encodesControlRequest() {
        val body = RequestJson.encodeToString(ControlRequest.serializer(), ControlRequest("seek", -10.0))
        assertEquals("""{"action":"seek","value":-10.0}""", body)
        val noValue = RequestJson.encodeToString(ControlRequest.serializer(), ControlRequest("toggle"))
        assertEquals("""{"action":"toggle"}""", noValue)
        val nullTheme = RequestJson.encodeToString(ScreensaverRequest.serializer(), ScreensaverRequest(null))
        assertEquals("""{"theme":null}""", nullTheme)
    }

    @Test
    fun spotifyPositionExtrapolatesFromUpdatedAt() {
        val sp = SpotifyState(active = true, playing = true, positionS = 30.0, durationS = 200.0, updatedAt = 1_000_000)
        assertEquals(40.0, sp.positionAt(1_010_000)!!, 0.001)
        assertEquals(30.0, sp.copy(playing = false).positionAt(1_010_000)!!, 0.001)
    }

    @Test
    fun parsesSpotifyControllableAndVolume() {
        val s = AppJson.decodeFromString(
            ServerState.serializer(),
            """{"spotify":{"active":true,"playing":false,"title":"T","volume":55,"controllable":true,"updated_at":1}}""",
        )
        assertTrue(s.spotify!!.controllable)
        assertEquals(55.0, s.spotify?.volume!!, 0.0)
        // Older hosts: neither field present; volume may also be explicitly null.
        val old = AppJson.decodeFromString(ServerState.serializer(), fullState).spotify!!
        assertFalse(old.controllable)
        assertNull(old.volume)
        val nullVol = AppJson.decodeFromString(SpotifyState.serializer(), """{"active":true,"volume":null,"controllable":true}""")
        assertNull(nullVol.volume)
    }

    @Test
    fun mediaTargetPrefersCastThenControllableSpotify() {
        val sp = SpotifyState(active = true, playing = true, controllable = true)
        assertEquals(MediaTarget.Cast, ServerState(activity = "playing", spotify = sp).mediaTarget)
        assertEquals(MediaTarget.Spotify, ServerState(activity = "idle", spotify = sp).mediaTarget)
        // A sleeping projector doesn't stop librespot, but the cast player is not a target then.
        assertEquals(MediaTarget.Spotify, ServerState(power = "sleeping", activity = "paused", spotify = sp).mediaTarget)
        assertEquals(MediaTarget.None, ServerState(activity = "idle", spotify = sp.copy(controllable = false)).mediaTarget)
        assertEquals(MediaTarget.None, ServerState(activity = "idle", spotify = sp.copy(active = false)).mediaTarget)
        assertEquals(MediaTarget.None, ServerState(activity = "idle").mediaTarget)
    }

    @Test
    fun spotifyRebaseFreezesExtrapolatedPosition() {
        val sp = SpotifyState(active = true, playing = true, positionS = 30.0, durationS = 200.0, updatedAt = 1_000_000)
        val paused = sp.rebased(nowMs = 1_005_000, playing = false)
        assertFalse(paused.playing)
        assertEquals(35.0, paused.positionS!!, 0.001)
        assertEquals(35.0, paused.positionAt(1_100_000)!!, 0.001)
        val seeked = sp.rebased(nowMs = 1_005_000, positionS = 100.0)
        assertEquals(101.0, seeked.positionAt(1_006_000)!!, 0.001)
    }

    @Test
    fun encodesJellyfinPlay() {
        assertEquals("""{"id":"a","resume":true}""", RequestJson.encodeToString(JfPlayRequest.serializer(), JfPlayRequest("a", true)))
        assertEquals("""{"id":"a","resume":false,"low_quality":true}""", RequestJson.encodeToString(JfPlayRequest.serializer(), JfPlayRequest("a", false, true)))
    }

    private fun night(json: String): ServerState = AppJson.decodeFromString(ServerState.serializer(), json)

    @Test
    fun nightAbsentOrNullIsNull() {
        assertNull(night("""{"power":"on"}""").night)
        assertNull(night("""{"power":"on","night":null}""").night)
    }

    @Test
    fun parsesFullNight() {
        val n = night(
            """{"night":{"enabled":true,"start":"23:00","end":"08:00","in_window":true,"pending":true,
                "next_check_at":1761343200000,"countdown_until":null}}""",
        ).night!!
        assertTrue(n.enabled)
        assertEquals("23:00", n.start)
        assertEquals("08:00", n.end)
        assertTrue(n.inWindow)
        assertTrue(n.pending)
        assertEquals(1761343200000, n.nextCheckAt)
        assertNull(n.countdownUntil)
        assertNull(n.countdownSecondsLeft(0))
        // 1761343200000 = 2025-10-24T22:00:00Z
        assertEquals("\uD83C\uDF19 Deep sleep 23:00\u201308:00 · pending · next check 22:00", n.chipLabel(ZoneOffset.UTC))
    }

    @Test
    fun toleratesPartialAndNullNightFields() {
        val n = night("""{"night":{"enabled":true,"start":null,"in_window":null,"next_check_at":null}}""").night!!
        assertTrue(n.enabled)
        assertNull(n.start)
        assertNull(n.end)
        assertFalse(n.inWindow)
        assertFalse(n.pending)
        assertNull(n.nextCheckAt)
        assertEquals("08:00", n.endLabel)
        assertEquals("\uD83C\uDF19 Deep sleep", n.chipLabel(ZoneOffset.UTC))

        val empty = night("""{"night":{}}""").night!!
        assertFalse(empty.enabled)
        assertNull(empty.chipLabel(ZoneOffset.UTC))
    }

    @Test
    fun chipShowsPendingOnlyInsideWindow() {
        val n = NightState(enabled = true, start = "23:00", end = "07:30", inWindow = false, pending = true)
        assertEquals("\uD83C\uDF19 Deep sleep 23:00\u201307:30", n.chipLabel(ZoneOffset.UTC))
        assertEquals("\uD83C\uDF19 Deep sleep 23:00\u201307:30 · pending", n.copy(inWindow = true).chipLabel(ZoneOffset.UTC))
    }

    @Test
    fun countdownSecondsLeft() {
        val n = night("""{"night":{"enabled":true,"countdown_until":1700000060000}}""").night!!
        assertEquals(1700000060000, n.countdownUntil)
        assertEquals(60L, n.countdownSecondsLeft(1700000000000))
        assertEquals(1L, n.countdownSecondsLeft(1700000059999))
        assertEquals(2L, n.countdownSecondsLeft(1700000058500))
        assertNull(n.countdownSecondsLeft(1700000060000))
        assertNull(n.countdownSecondsLeft(1700000099000))
    }
}
