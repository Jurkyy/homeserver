package dev.woto.homeserver.share

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import dev.woto.homeserver.MainActivity
import dev.woto.homeserver.R
import dev.woto.homeserver.data.userMessage
import dev.woto.homeserver.graph
import kotlinx.coroutines.launch

/** Invisible activity: receives a shared URL (e.g. from the YouTube app), casts it, toasts, and finishes. */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val g = applicationContext.graph
        val text = intent?.takeIf { it.action == Intent.ACTION_SEND }?.getStringExtra(Intent.EXTRA_TEXT)
            ?: intent?.dataString
        val url = ShareParsing.extractUrl(text)
        when {
            !g.settings.current.isPaired -> {
                Toast.makeText(this, R.string.share_not_paired, Toast.LENGTH_LONG).show()
                startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                finish()
            }
            url == null -> {
                Toast.makeText(this, R.string.share_no_url, Toast.LENGTH_LONG).show()
                finish()
            }
            else -> {
                val appCtx = applicationContext
                // Cast on the app scope so it completes even after this activity finishes.
                g.scope.launch {
                    val result = g.actions.cast(url)
                    val msg = result.fold(
                        onSuccess = { appCtx.getString(R.string.share_casting) },
                        onFailure = { appCtx.getString(R.string.share_failed, it.userMessage()) },
                    )
                    Toast.makeText(appCtx, msg, Toast.LENGTH_SHORT).show()
                }
                finish()
            }
        }
    }
}

object ShareParsing {
    private val urlRegex = Regex("""https?://[^\s<>"']+""", RegexOption.IGNORE_CASE)

    /** Pulls the first http(s) URL out of shared text (apps often share "Title https://…"). */
    fun extractUrl(text: String?): String? {
        if (text.isNullOrBlank()) return null
        return urlRegex.find(text)?.value?.trimEnd('.', ',', ')', ']', '!', '?')
    }
}
