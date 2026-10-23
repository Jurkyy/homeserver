package dev.woto.homeserver.data

import java.net.URI
import java.net.URLDecoder
import java.nio.charset.StandardCharsets

data class PairingInfo(val baseUrl: String, val token: String)

object Pairing {
    const val SCHEME = "mediacast"

    /**
     * Parses `mediacast://pair?url=<url-encoded base url>&token=<token>`.
     * Returns null for anything that is not a valid pairing URI.
     */
    fun parse(raw: String?): PairingInfo? {
        val text = raw?.trim().orEmpty()
        if (text.isEmpty()) return null
        val uri = runCatching { URI(text) }.getOrNull() ?: return null
        if (!uri.scheme.equals(SCHEME, ignoreCase = true)) return null
        // mediacast://pair?... -> host = pair ; mediacast:pair?... -> schemeSpecificPart starts with pair
        val target = uri.host ?: uri.schemeSpecificPart?.substringBefore('?')?.trimStart('/')
        if (!target.equals("pair", ignoreCase = true)) return null
        val query = uri.rawQuery ?: uri.schemeSpecificPart?.substringAfter('?', "") ?: return null
        val params = parseQuery(query)
        val url = params["url"]?.let(::normalizeBaseUrl) ?: return null
        val token = params["token"]?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        return PairingInfo(url, token)
    }

    private fun parseQuery(query: String): Map<String, String> =
        query.split('&').filter { it.isNotEmpty() }.associate { part ->
            val k = part.substringBefore('=')
            val v = part.substringAfter('=', "")
            decode(k) to decode(v)
        }

    private fun decode(s: String): String = URLDecoder.decode(s, StandardCharsets.UTF_8.name())

    /** Adds a scheme if missing and strips trailing slashes / a trailing /api/v1. Returns null if not an http(s) URL. */
    fun normalizeBaseUrl(input: String): String? {
        var s = input.trim()
        if (s.isEmpty()) return null
        if (!s.contains("://")) s = "http://$s"
        s = s.trimEnd('/')
        if (s.endsWith("/api/v1")) s = s.removeSuffix("/api/v1").trimEnd('/')
        val uri = runCatching { URI(s) }.getOrNull() ?: return null
        val scheme = uri.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return null
        if (uri.host.isNullOrBlank()) return null
        return s
    }
}

object Urls {
    /** Resolves a possibly-relative URL (e.g. "/api/v1/artwork/x") against the base URL. */
    fun resolve(baseUrl: String?, path: String?): String? {
        if (path.isNullOrBlank()) return null
        if (path.startsWith("http://") || path.startsWith("https://")) return path
        val base = baseUrl?.trimEnd('/') ?: return null
        return if (path.startsWith("/")) {
            // Absolute path: resolve against the origin of the base URL.
            val origin = runCatching {
                val u = URI(base)
                URI(u.scheme, null, u.host, u.port, null, null, null).toString()
            }.getOrDefault(base)
            // If the base has a path prefix (reverse proxy sub-path), keep it.
            val prefix = runCatching { URI(base).path.orEmpty().trimEnd('/') }.getOrDefault("")
            if (prefix.isNotEmpty() && !path.startsWith(prefix)) "$origin$prefix$path" else "$origin$path"
        } else {
            "$base/$path"
        }
    }
}
