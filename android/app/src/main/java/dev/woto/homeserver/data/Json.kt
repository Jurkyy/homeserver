package dev.woto.homeserver.data

import kotlinx.serialization.json.Json

val AppJson: Json = Json {
    ignoreUnknownKeys = true
    coerceInputValues = true
    explicitNulls = false
    isLenient = true
    encodeDefaults = true
}

/** Encoder for request bodies: nulls are explicit (e.g. `{"theme": null}` stops the screensaver). */
val RequestJson: Json = Json {
    explicitNulls = true
    encodeDefaults = true
}
