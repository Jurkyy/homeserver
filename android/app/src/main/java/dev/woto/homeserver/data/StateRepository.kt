package dev.woto.homeserver.data

import android.os.SystemClock
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import java.util.concurrent.TimeUnit
import kotlin.math.min

enum class ConnectionStatus { Idle, Connecting, Connected, Reconnecting, Offline, Unauthorized }

/** A state plus the local monotonic time it was received at, so positions can be interpolated. */
data class StateSnapshot(val state: ServerState, val receivedAtMs: Long) {
    /** Best estimate of the current playback position in seconds. */
    fun positionAt(nowMs: Long = SystemClock.elapsedRealtime()): Double? {
        val p = state.positionS ?: return null
        val pos = if (state.isPlaying) p + (nowMs - receivedAtMs) / 1000.0 else p
        val d = state.durationS
        return if (d != null && d > 0) pos.coerceIn(0.0, d) else pos.coerceAtLeast(0.0)
    }
}

/**
 * Holds live projector state, fed by the SSE stream. The stream runs only while someone wants it
 * (app in foreground, media notification active, …) — see [acquire].
 */
class StateRepository(
    private val api: ApiClient,
    private val settings: SettingsStore,
    private val scope: CoroutineScope,
) {
    private val _snapshot = MutableStateFlow<StateSnapshot?>(null)
    val snapshot: StateFlow<StateSnapshot?> = _snapshot.asStateFlow()

    private val _status = MutableStateFlow(ConnectionStatus.Idle)
    val status: StateFlow<ConnectionStatus> = _status.asStateFlow()

    private val _feedSignal = MutableStateFlow<FeedSignal?>(null)
    /** Latest `event: feed` from the server (re-sent on every connect), or null if none seen yet / old server. */
    val feedSignal: StateFlow<FeedSignal?> = _feedSignal.asStateFlow()

    private val holders = MutableStateFlow<Set<String>>(emptySet())
    private var job: Job? = null

    private val sseClient by lazy {
        api.http.newBuilder()
            .readTimeout(45, TimeUnit.SECONDS) // server pings every 15 s
            .retryOnConnectionFailure(false)
            .build()
    }

    val state: ServerState? get() = _snapshot.value?.state

    init {
        scope.launch {
            combine(holders.map { it.isNotEmpty() }, settings.settings.map { it.credentials }) { wanted, creds -> wanted to creds }
                .distinctUntilChanged()
                .collect { (wanted, creds) ->
                    job?.cancel()
                    job = null
                    if (wanted && creds != null) {
                        job = scope.launch { runLoop() }
                    } else {
                        _status.value = if (creds == null) ConnectionStatus.Idle else ConnectionStatus.Idle
                    }
                }
        }
    }

    /** Registers interest in live updates under [key]. Connection stays open while at least one key is held. */
    fun acquire(key: String) = holders.update { it + key }
    fun release(key: String) = holders.update { it - key }

    private inline fun MutableStateFlow<Set<String>>.update(f: (Set<String>) -> Set<String>) {
        while (true) {
            val prev = value
            if (compareAndSet(prev, f(prev))) return
        }
    }

    /** Apply a state we fetched some other way (e.g. after a command). */
    fun push(state: ServerState) {
        _snapshot.value = StateSnapshot(state, SystemClock.elapsedRealtime())
    }

    /** Optimistic local tweak so the UI feels instant; the next server event overwrites it. */
    fun optimistic(transform: (ServerState) -> ServerState) {
        val cur = _snapshot.value ?: return
        _snapshot.value = StateSnapshot(transform(cur.state), SystemClock.elapsedRealtime()).let {
            // keep interpolation continuous
            if (cur.state.isPlaying && it.state.positionS == cur.state.positionS) it.copy(receivedAtMs = cur.receivedAtMs) else it
        }
    }

    suspend fun refresh(): Result<ServerState> = runCatching { api.state() }.onSuccess { push(it) }

    private suspend fun runLoop() {
        var attempt = 0
        while (scope.isActive) {
            _status.value = if (attempt == 0) ConnectionStatus.Connecting else ConnectionStatus.Reconnecting
            val result = streamOnce { attempt = 0 }
            if (result == StreamEnd.Unauthorized) {
                _status.value = ConnectionStatus.Unauthorized
                delay(30_000)
                continue
            }
            attempt++
            val backoff = min(30_000L, 1_000L * (1L shl min(attempt - 1, 5)))
            _status.value = if (attempt >= 4) ConnectionStatus.Offline else ConnectionStatus.Reconnecting
            delay(backoff)
        }
    }

    private enum class StreamEnd { Closed, Failed, Unauthorized }

    private suspend fun streamOnce(onConnected: () -> Unit): StreamEnd {
        val request = api.eventsRequest() ?: return StreamEnd.Failed
        var end = StreamEnd.Closed
        callbackFlow<Unit> {
            val listener = object : EventSourceListener() {
                override fun onOpen(eventSource: EventSource, response: Response) {
                    _status.value = ConnectionStatus.Connected
                    onConnected()
                }

                override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                    when (val ev = SseEvents.decode(type, data)) {
                        is ServerEvent.State -> push(ev.state)
                        is ServerEvent.Feed -> _feedSignal.value = ev.signal
                        else -> Unit
                    }
                }

                override fun onClosed(eventSource: EventSource) {
                    end = StreamEnd.Closed
                    close()
                }

                override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
                    end = if (response?.code == 401 || response?.code == 403) StreamEnd.Unauthorized else StreamEnd.Failed
                    if (t != null) Log.d(TAG, "SSE failure: ${t.message}")
                    close()
                }
            }
            val source = EventSources.createFactory(sseClient).newEventSource(request, listener)
            awaitClose { source.cancel() }
        }.collect { }
        return end
    }

    companion object {
        private const val TAG = "StateRepository"
    }
}
