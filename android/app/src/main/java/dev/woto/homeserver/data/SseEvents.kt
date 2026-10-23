package dev.woto.homeserver.data

/** A decoded server-sent event from `/api/v1/events`. */
sealed interface ServerEvent {
    data class State(val state: ServerState) : ServerEvent
    data class Unknown(val type: String?, val data: String) : ServerEvent
}

object SseEvents {
    /**
     * Decodes an SSE event. `event: state` (or an unnamed event whose payload parses as a state) yields [ServerEvent.State].
     * Malformed payloads return null so the stream keeps going.
     */
    fun decode(type: String?, data: String): ServerEvent? {
        val payload = data.trim()
        if (payload.isEmpty()) return null
        return when (type) {
            "state", null, "", "message" -> runCatching { ServerEvent.State(AppJson.decodeFromString(ServerState.serializer(), payload)) }
                .getOrElse { if (type == "state") null else ServerEvent.Unknown(type, payload) }
            else -> ServerEvent.Unknown(type, payload)
        }
    }

    /**
     * Minimal SSE text/event-stream parser (used for tests and as a fallback). Feeds lines, emits complete events
     * on blank lines. Comment lines (": ping") are ignored.
     */
    class LineParser(private val onEvent: (type: String?, data: String) -> Unit) {
        private var type: String? = null
        private val data = StringBuilder()

        fun feed(line: String) {
            val l = line.removeSuffix("\r")
            when {
                l.isEmpty() -> dispatch()
                l.startsWith(":") -> Unit
                else -> {
                    val field = l.substringBefore(':')
                    var value = if (l.contains(':')) l.substringAfter(':') else ""
                    if (value.startsWith(" ")) value = value.substring(1)
                    when (field) {
                        "event" -> type = value
                        "data" -> { if (data.isNotEmpty()) data.append('\n'); data.append(value) }
                        else -> Unit // id / retry ignored
                    }
                }
            }
        }

        private fun dispatch() {
            if (data.isNotEmpty() || type != null) onEvent(type, data.toString())
            type = null
            data.setLength(0)
        }
    }
}
