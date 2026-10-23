package dev.woto.homeserver.data

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.runBlocking

private val Context.dataStore by preferencesDataStore(name = "settings")

data class AppSettings(
    val baseUrl: String? = null,
    val token: String? = null,
    val mediaNotification: Boolean = true,
    val dynamicColor: Boolean = true,
    val hideShorts: Boolean = false,
    val volumeKeys: Boolean = true,
) {
    val credentials: Credentials? get() = if (!baseUrl.isNullOrBlank() && !token.isNullOrBlank()) Credentials(baseUrl, token) else null
    val isPaired: Boolean get() = credentials != null
}

/**
 * Settings persisted in DataStore. The server token lives here too: the app is LAN-only, the data store is in
 * app-private storage, and Android's file-based encryption protects it at rest.
 */
class SettingsStore(private val context: Context, scope: CoroutineScope) {
    private object Keys {
        val baseUrl = stringPreferencesKey("base_url")
        val token = stringPreferencesKey("token")
        val mediaNotification = booleanPreferencesKey("media_notification")
        val dynamicColor = booleanPreferencesKey("dynamic_color")
        val hideShorts = booleanPreferencesKey("hide_shorts")
        val volumeKeys = booleanPreferencesKey("volume_keys")
    }

    private fun Preferences.toSettings() = AppSettings(
        baseUrl = this[Keys.baseUrl],
        token = this[Keys.token],
        mediaNotification = this[Keys.mediaNotification] ?: true,
        dynamicColor = this[Keys.dynamicColor] ?: true,
        hideShorts = this[Keys.hideShorts] ?: false,
        volumeKeys = this[Keys.volumeKeys] ?: true,
    )

    // Read once synchronously at startup so the first frame knows whether we're paired (tiny file, fast).
    private val initial: AppSettings = runBlocking { context.dataStore.data.first().toSettings() }

    val settings: StateFlow<AppSettings> = context.dataStore.data
        .map { it.toSettings() }
        .stateIn(scope, SharingStarted.Eagerly, initial)

    val current: AppSettings get() = settings.value

    suspend fun pair(baseUrl: String, token: String) = context.dataStore.edit {
        it[Keys.baseUrl] = baseUrl
        it[Keys.token] = token
    }

    suspend fun unpair() = context.dataStore.edit {
        it.remove(Keys.token)
    }

    suspend fun setMediaNotification(v: Boolean) = context.dataStore.edit { it[Keys.mediaNotification] = v }
    suspend fun setDynamicColor(v: Boolean) = context.dataStore.edit { it[Keys.dynamicColor] = v }
    suspend fun setHideShorts(v: Boolean) = context.dataStore.edit { it[Keys.hideShorts] = v }
    suspend fun setVolumeKeys(v: Boolean) = context.dataStore.edit { it[Keys.volumeKeys] = v }
}
