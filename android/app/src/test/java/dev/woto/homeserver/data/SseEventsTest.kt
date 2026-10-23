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
