package dev.woto.homeserver.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class ApiException(message: String, val status: Int = 0, val code: String? = null) : IOException(message)

data class Credentials(val baseUrl: String, val token: String)

/**
 * Thin typed client for the mediacast API v1. Credentials are read lazily so re-pairing takes effect immediately.
 */
class ApiClient(
    val http: OkHttpClient,
    private val credentials: () -> Credentials?,
) {
    private val jsonType = "application/json; charset=utf-8".toMediaType()

    private fun creds(): Credentials = credentials() ?: throw ApiException("Not paired with a server", code = "not_paired")

    private fun url(base: String, path: String, query: Map<String, String?> = emptyMap()): HttpUrl {
        val root = base.trimEnd('/').toHttpUrlOrNull() ?: throw ApiException("Invalid server URL: $base", code = "bad_url")
        val b = root.newBuilder()
        "api/v1/$path".split('/').filter { it.isNotEmpty() }.forEach { b.addPathSegment(it) }
        query.forEach { (k, v) -> if (v != null) b.addQueryParameter(k, v) }
        return b.build()
    }

    private suspend fun <T> get(path: String, ser: KSerializer<T>, query: Map<String, String?> = emptyMap(), auth: Boolean = true, override: Credentials? = null): T {
        val c = override ?: creds()
        val req = Request.Builder().url(url(c.baseUrl, path, query)).get()
        if (auth) req.header("Authorization", "Bearer ${c.token}")
        return execute(req.build(), ser)
    }

    private suspend fun <B, T> post(path: String, bodySer: KSerializer<B>, body: B, ser: KSerializer<T>): T {
        val c = creds()
        val json = RequestJson.encodeToString(bodySer, body)
        val req = Request.Builder()
            .url(url(c.baseUrl, path))
            .header("Authorization", "Bearer ${c.token}")
            .post(json.toRequestBody(jsonType))
            .build()
        return execute(req, ser)
    }

    private suspend fun <T> execute(request: Request, ser: KSerializer<T>): T = withContext(Dispatchers.IO) {
        http.newCall(request).await().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                val err = runCatching { AppJson.decodeFromString(ErrorResponse.serializer(), text) }.getOrNull()
                val msg = err?.error ?: when (resp.code) {
                    401, 403 -> "Unauthorized — check the token"
                    404 -> "Not found (is the server up to date?)"
                    else -> "Server error ${resp.code}"
                }
                throw ApiException(msg, resp.code, err?.code)
            }
            try {
                AppJson.decodeFromString(ser, text.ifBlank { "{}" })
            } catch (e: Exception) {
                throw ApiException("Unexpected response from server", resp.code, "bad_json")
            }
        }
    }

    // --- Pairing helpers -------------------------------------------------------------------------------------

    suspend fun health(baseUrl: String): HealthResponse =
        get("health", HealthResponse.serializer(), auth = false, override = Credentials(baseUrl, ""))

    suspend fun stateWith(credentials: Credentials): ServerState =
        get("state", ServerState.serializer(), override = credentials)

    // --- State / playback ------------------------------------------------------------------------------------

    suspend fun state(): ServerState = get("state", ServerState.serializer())

    suspend fun cast(url: String) = post("cast", CastRequest.serializer(), CastRequest(url), OkResponse.serializer())

    suspend fun control(action: String, value: Double? = null) =
        post("control", ControlRequest.serializer(), ControlRequest(action, value), OkResponse.serializer())

    suspend fun screensaver(theme: String?) =
        post("screensaver", ScreensaverRequest.serializer(), ScreensaverRequest(theme), OkResponse.serializer())

    suspend fun power(action: String) = post("power", PowerRequest.serializer(), PowerRequest(action), OkResponse.serializer())

    // --- YouTube ---------------------------------------------------------------------------------------------

    suspend fun youtubeFeed(refresh: Boolean, shorts: Boolean = true): YouTubeFeed =
        get("youtube/feed", YouTubeFeed.serializer(), mapOf("refresh" to if (refresh) "1" else "0", "shorts" to if (shorts) "1" else "0"))

    // --- Jellyfin --------------------------------------------------------------------------------------------

    suspend fun jfViews(): JfList = get("jellyfin/views", JfList.serializer())

    suspend fun jfItems(parentId: String? = null, search: String? = null, start: Int = 0, limit: Int = 60, types: String? = null): JfList =
        get(
            "jellyfin/items", JfList.serializer(),
            mapOf("parent_id" to parentId, "search" to search, "start" to start.toString(), "limit" to limit.toString(), "types" to types),
        )

    suspend fun jfResume(): JfList = get("jellyfin/resume", JfList.serializer())
    suspend fun jfNextUp(): JfList = get("jellyfin/nextup", JfList.serializer())
    suspend fun jfItem(id: String): JfItem = get("jellyfin/item/$id", JfItem.serializer())
    suspend fun jfPlay(id: String, resume: Boolean) =
        post("jellyfin/play", JfPlayRequest.serializer(), JfPlayRequest(id, resume), OkResponse.serializer())

    /** Request for the SSE stream (long-lived). */
    fun eventsRequest(): Request? {
        val c = credentials() ?: return null
        return Request.Builder()
            .url(url(c.baseUrl, "events"))
            .header("Authorization", "Bearer ${c.token}")
            .header("Accept", "text/event-stream")
            .build()
    }

    companion object {
        fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(10, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }
}

private suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
    enqueue(object : Callback {
        override fun onResponse(call: Call, response: Response) = cont.resume(response)
        override fun onFailure(call: Call, e: IOException) {
            if (!cont.isCancelled) cont.resumeWithException(e)
        }
    })
    cont.invokeOnCancellation { runCatching { cancel() } }
}

/** Human-friendly message for any network failure. */
fun Throwable.userMessage(): String = when (this) {
    is ApiException -> message ?: "Request failed"
    is java.net.UnknownHostException -> "Can't resolve server host"
    is java.net.ConnectException -> "Can't reach the server"
    is java.net.SocketTimeoutException -> "Server timed out"
    is IOException -> message ?: "Network error"
    else -> message ?: toString()
}
