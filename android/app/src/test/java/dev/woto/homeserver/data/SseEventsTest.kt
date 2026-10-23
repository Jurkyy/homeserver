package dev.woto.homeserver.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SseEventsTest {

    @Test
    fun decodesStateEvent() {
        val ev = SseEvents.decode("state", """{"power":"on","activity":"paused","title":"T","volume":55,"unknown":1}""")
        assertTrue(ev is ServerEvent.State)
        val s = (ev as ServerEvent.State).state
        assertEquals("T", s.title)
        assertEquals(55.0, s.volume, 0.0)
    }

    @Test
    fun malformedStateIsIgnored() {
        assertNull(SseEvents.decode("state", "{not json"))
        assertNull(SseEvents.decode("state", "   "))
    }

    @Test
    fun unknownEventTypesPassThrough() {
        val ev = SseEvents.decode("toast", """{"msg":"hi"}""")
        assertTrue(ev is ServerEvent.Unknown)
    }

    @Test
    fun decodesFeedEvent() {
        val ev = SseEvents.decode("feed", """{"updated_at":1761200000000,"new_count":3,"removed_count":1,"extra":true}""")
        assertTrue(ev is ServerEvent.Feed)
        val sig = (ev as ServerEvent.Feed).signal
        assertEquals(1761200000000L, sig.updatedAt)
        assertEquals(3, sig.newCount)
        assertEquals(1, sig.removedCount)
        // counts are optional (connect-time event may omit them)
        assertEquals(FeedSignal(updatedAt = 5), (SseEvents.decode("feed", """{"updated_at":5}""") as ServerEvent.Feed).signal)
    }

    @Test
    fun malformedFeedIsIgnored() {
        assertNull(SseEvents.decode("feed", "{not json"))
        assertNull(SseEvents.decode("feed", """{"updated_at":"soon"}"""))
        assertNull(SseEvents.decode("feed", """{"new_count":2}""")) // no usable timestamp
        assertNull(SseEvents.decode("feed", "[]"))
        assertNull(SseEvents.decode("feed", ""))
    }

    @Test
    fun unknownEventsAroundFeedDoNotBreakTheStream() {
        val events = mutableListOf<ServerEvent?>()
        val parser = SseEvents.LineParser { t, d -> events += SseEvents.decode(t, d) }
        val stream = "event: state\ndata: {\"power\":\"on\"}\n\n" +
            "event: feed\ndata: {\"updated_at\":42,\"new_count\":0,\"removed_count\":0}\n\n" +
            "event: shiny\ndata: whatever\n\n" +
            "event: feed\ndata: garbage\n\n" +
            "event: feed\ndata: {\"updated_at\":43,\"new_count\":2,\"removed_count\":0}\n\n"
        stream.split("\n").forEach(parser::feed)
        assertEquals(5, events.size)
        assertTrue(events[0] is ServerEvent.State)
        assertEquals(42L, (events[1] as ServerEvent.Feed).signal.updatedAt)
        assertTrue(events[2] is ServerEvent.Unknown)
        assertNull(events[3])
        assertEquals(2, (events[4] as ServerEvent.Feed).signal.newCount)
    }

    @Test
    fun feedModelToleratesMissingChangedAt() {
        val old = AppJson.decodeFromString(YouTubeFeed.serializer(), """{"items":[],"updated_at":10}""")
        assertEquals(0L, old.changedAt)
        val new = AppJson.decodeFromString(YouTubeFeed.serializer(), """{"items":[],"updated_at":10,"changed_at":7}""")
        assertEquals(7L, new.changedAt)
    }

    @Test
    fun lineParserHandlesStreamWithPingsAndMultilineData() {
        val events = mutableListOf<Pair<String?, String>>()
        val parser = SseEvents.LineParser { t, d -> events += t to d }
        val stream = """
            : ping

            event: state
            data: {"power":"on",
            data: "activity":"playing"}

            : ping
            event: state
            data:{"power":"sleeping"}

        """.trimIndent() + "\n\n"
        stream.split("\n").forEach(parser::feed)
        assertEquals(2, events.size)
        assertEquals("state", events[0].first)
        assertEquals("{\"power\":\"on\",\n\"activity\":\"playing\"}", events[0].second)
        val first = SseEvents.decode(events[0].first, events[0].second) as ServerEvent.State
        assertTrue(first.state.isPlaying)
        val second = SseEvents.decode(events[1].first, events[1].second) as ServerEvent.State
        assertTrue(second.state.isSleeping)
    }

    @Test
    fun snapshotInterpolatesWhilePlaying() {
        val playing = StateSnapshot(ServerState(activity = "playing", positionS = 10.0, durationS = 100.0), receivedAtMs = 1_000)
        assertEquals(15.0, playing.positionAt(6_000)!!, 0.001)
        assertEquals(100.0, playing.positionAt(1_000_000)!!, 0.001) // clamped to duration
        val paused = StateSnapshot(ServerState(activity = "paused", positionS = 10.0, durationS = 100.0), receivedAtMs = 1_000)
        assertEquals(10.0, paused.positionAt(6_000)!!, 0.001)
        assertNull(StateSnapshot(ServerState(), 0).positionAt(5))
    }
}
