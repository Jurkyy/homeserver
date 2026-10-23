package dev.woto.homeserver

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.woto.homeserver.data.Pairing
import dev.woto.homeserver.data.PairingInfo
import dev.woto.homeserver.ui.AppRoot
import dev.woto.homeserver.ui.theme.HomeserverTheme
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class MainActivity : ComponentActivity() {
    private val pendingPair = MutableStateFlow<PairingInfo?>(null)
    private val _volumeHud = MutableStateFlow(0L)
    /** Bumped whenever a hardware volume key adjusted server volume (drives the on-screen HUD). */
    val volumeHud: StateFlow<Long> = _volumeHud.asStateFlow()

    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        handleIntent(intent)
        val g = graph
        setContent {
            val settings by g.settings.settings.collectAsStateWithLifecycle()
            HomeserverTheme(dynamicColor = settings.dynamicColor) {
                AppRoot(
                    graph = g,
                    pendingPair = pendingPair,
                    onPairConsumed = { pendingPair.value = null },
                    volumeHud = volumeHud,
                    onPaired = { maybeAskNotificationPermission() },
                )
            }
        }
        if (g.settings.current.isPaired) maybeAskNotificationPermission()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        Pairing.parse(intent?.dataString)?.let { pendingPair.value = it }
    }

    private fun maybeAskNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            graph.settings.current.mediaNotification &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        val delta = when (keyCode) {
            KeyEvent.KEYCODE_VOLUME_UP -> VOLUME_STEP
            KeyEvent.KEYCODE_VOLUME_DOWN -> -VOLUME_STEP
            else -> return super.onKeyDown(keyCode, event)
        }
        val g = graph
        val s = g.settings.current
        if (!s.isPaired || !s.volumeKeys || g.repository.state == null) return super.onKeyDown(keyCode, event)
        g.actions.volumeDelta(delta)
        _volumeHud.value = System.currentTimeMillis()
        return true
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_UP || keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            val g = graph
            if (g.settings.current.isPaired && g.settings.current.volumeKeys && g.repository.state != null) return true
        }
        return super.onKeyUp(keyCode, event)
    }

    companion object {
        const val VOLUME_STEP = 5.0
    }
}
