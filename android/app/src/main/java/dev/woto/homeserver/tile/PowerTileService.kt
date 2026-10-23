package dev.woto.homeserver.tile

import android.graphics.drawable.Icon
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import dev.woto.homeserver.R
import dev.woto.homeserver.data.ServerState
import dev.woto.homeserver.graph
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/** Quick Settings tile: tap to put the projector to sleep / wake it. Active = projector awake. */
class PowerTileService : TileService() {
    private var scope: CoroutineScope? = null
    private var busy: Job? = null

    override fun onStartListening() {
        super.onStartListening()
        val s = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        scope = s
        render(graph.repository.state, loading = false)
        s.launch {
            val result = graph.repository.refresh()
            render(result.getOrNull(), loading = false, offline = result.isFailure)
        }
    }

    override fun onStopListening() {
        scope?.cancel()
        scope = null
        super.onStopListening()
    }

    override fun onClick() {
        super.onClick()
        val s = scope ?: return
        if (busy?.isActive == true) return
        val g = graph
        if (!g.settings.current.isPaired) return
        busy = s.launch {
            val current = g.repository.state ?: g.repository.refresh().getOrNull()
            val action = if (current?.isSleeping == true) "wake" else "sleep"
            render(current?.copy(power = if (action == "wake") "on" else "sleeping"), loading = true)
            val ok = runCatching { g.api.power(action) }.isSuccess
            val fresh = g.repository.refresh().getOrNull()
            render(fresh ?: current, loading = false, offline = !ok && fresh == null)
        }
    }

    private fun render(state: ServerState?, loading: Boolean, offline: Boolean = false) {
        val tile = qsTile ?: return
        val paired = graph.settings.current.isPaired
        tile.icon = Icon.createWithResource(this, R.drawable.ic_stat_projector)
        tile.label = getString(R.string.tile_label)
        when {
            !paired -> {
                tile.state = Tile.STATE_UNAVAILABLE
                setSubtitle(tile, getString(R.string.tile_not_paired))
            }
            state == null -> {
                tile.state = Tile.STATE_UNAVAILABLE.takeIf { offline } ?: Tile.STATE_INACTIVE
                setSubtitle(tile, getString(if (offline) R.string.tile_offline else R.string.tile_unknown))
            }
            else -> {
                tile.state = if (state.isSleeping) Tile.STATE_INACTIVE else Tile.STATE_ACTIVE
                setSubtitle(
                    tile,
                    when {
                        loading -> getString(R.string.tile_working)
                        state.isSleeping -> getString(R.string.tile_sleeping)
                        state.hasMedia -> state.title ?: getString(R.string.tile_awake)
                        else -> getString(R.string.tile_awake)
                    },
                )
            }
        }
        tile.updateTile()
    }

    private fun setSubtitle(tile: Tile, text: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) tile.subtitle = text
    }
}
