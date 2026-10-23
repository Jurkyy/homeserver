package dev.woto.homeserver.ui.pairing

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.CheckCircle
import androidx.compose.material.icons.rounded.ErrorOutline
import androidx.compose.material.icons.rounded.Link
import androidx.compose.material.icons.rounded.Key
import androidx.compose.material.icons.rounded.QrCodeScanner
import androidx.compose.material.icons.rounded.Visibility
import androidx.compose.material.icons.rounded.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.woto.homeserver.AppGraph
import dev.woto.homeserver.R
import dev.woto.homeserver.data.Credentials
import dev.woto.homeserver.data.Pairing
import dev.woto.homeserver.data.PairingInfo
import dev.woto.homeserver.data.userMessage
import dev.woto.homeserver.ui.components.rememberHaptic
import kotlinx.coroutines.launch

private sealed interface PairStatus {
    data object Idle : PairStatus
    data class Working(val step: String) : PairStatus
    data class Failed(val message: String) : PairStatus
    data class Success(val version: String?) : PairStatus
}

val DefaultUrls = listOf("http://homeserver.local", "http://192.168.0.22")

@Composable
fun PairingScreen(
    graph: AppGraph,
    prefill: PairingInfo?,
    onPrefillConsumed: () -> Unit,
    onScan: () -> Unit,
    canGoBack: Boolean,
    onBack: () -> Unit,
    onPaired: () -> Unit,
) {
    val current = graph.settings.current
    var url by rememberSaveable { mutableStateOf(current.baseUrl ?: DefaultUrls.first()) }
    var token by rememberSaveable { mutableStateOf(current.token.orEmpty()) }
    var showToken by rememberSaveable { mutableStateOf(false) }
    var status by remember { mutableStateOf<PairStatus>(PairStatus.Idle) }
    val scope = rememberCoroutineScope()
    val haptic = rememberHaptic()

    fun connect() {
        val base = Pairing.normalizeBaseUrl(url)
        if (base == null) { status = PairStatus.Failed("Enter a valid http(s) URL"); return }
        if (token.isBlank()) { status = PairStatus.Failed("Enter the token (MEDIACAST_TOKEN)"); return }
        scope.launch {
            status = PairStatus.Working("Contacting $base…")
            val health = runCatching { graph.api.health(base) }.getOrElse {
                status = PairStatus.Failed("Server not reachable: ${it.userMessage()}"); return@launch
            }
            status = PairStatus.Working("Checking token…")
            val creds = Credentials(base, token.trim())
            val state = runCatching { graph.api.stateWith(creds) }.getOrElse {
                status = PairStatus.Failed(it.userMessage()); return@launch
            }
            graph.settings.pair(base, token.trim())
            graph.repository.push(state)
            haptic(true)
            status = PairStatus.Success(health.version)
            onPaired()
        }
    }

    LaunchedEffect(prefill) {
        if (prefill != null) {
            url = prefill.baseUrl
            token = prefill.token
            onPrefillConsumed()
            connect()
        }
    }

    Column(
        Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing).imePadding().verticalScroll(rememberScrollState()),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(Modifier.fillMaxWidth().height(56.dp), verticalAlignment = Alignment.CenterVertically) {
            if (canGoBack) IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Back") }
        }
        Column(
            Modifier.widthIn(max = 520.dp).fillMaxWidth().padding(horizontal = 24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Image(painterResource(R.drawable.ic_launcher_foreground), null, Modifier.size(120.dp))
            Text("Connect to your projector", style = MaterialTheme.typography.headlineSmall, textAlign = TextAlign.Center)
            Text(
                "Open the Homeserver web portal on a computer, choose “Pair phone”, and scan the QR code.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center, modifier = Modifier.padding(top = 8.dp),
            )
            Button(onClick = onScan, modifier = Modifier.fillMaxWidth().padding(top = 24.dp).height(56.dp)) {
                Icon(Icons.Rounded.QrCodeScanner, null)
                Spacer(Modifier.width(10.dp))
                Text("Scan pairing QR code")
            }

            Row(Modifier.fillMaxWidth().padding(vertical = 24.dp), verticalAlignment = Alignment.CenterVertically) {
                HorizontalDivider(Modifier.weight(1f))
                Text("or enter manually", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 12.dp))
                HorizontalDivider(Modifier.weight(1f))
            }

            OutlinedTextField(
                value = url, onValueChange = { url = it; status = PairStatus.Idle },
                label = { Text("Server URL") },
                leadingIcon = { Icon(Icons.Rounded.Link, null) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next),
                modifier = Modifier.fillMaxWidth(),
            )
            Row(Modifier.fillMaxWidth().padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                DefaultUrls.forEach { u ->
                    SuggestionChip(onClick = { url = u; status = PairStatus.Idle }, label = { Text(u.removePrefix("http://")) })
                }
            }
            OutlinedTextField(
                value = token, onValueChange = { token = it; status = PairStatus.Idle },
                label = { Text("Token") },
                leadingIcon = { Icon(Icons.Rounded.Key, null) },
                trailingIcon = {
                    IconButton(onClick = { showToken = !showToken }) {
                        Icon(if (showToken) Icons.Rounded.VisibilityOff else Icons.Rounded.Visibility, if (showToken) "Hide token" else "Show token")
                    }
                },
                visualTransformation = if (showToken) VisualTransformation.None else PasswordVisualTransformation(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )

            AnimatedVisibility(status !is PairStatus.Idle) {
                StatusCard(status, Modifier.padding(top = 16.dp))
            }

            OutlinedButton(
                onClick = ::connect,
                enabled = status !is PairStatus.Working,
                modifier = Modifier.fillMaxWidth().padding(top = 16.dp, bottom = 32.dp).height(52.dp),
            ) { Text("Connect") }
        }
    }
}

@Composable
private fun StatusCard(status: PairStatus, modifier: Modifier = Modifier) {
    val (color, text) = when (status) {
        is PairStatus.Working -> MaterialTheme.colorScheme.surfaceContainerHigh to status.step
        is PairStatus.Failed -> MaterialTheme.colorScheme.errorContainer to status.message
        is PairStatus.Success -> MaterialTheme.colorScheme.primaryContainer to ("Connected" + (status.version?.let { " · server $it" } ?: ""))
        PairStatus.Idle -> MaterialTheme.colorScheme.surface to ""
    }
    Card(modifier.fillMaxWidth(), shape = RoundedCornerShape(16.dp), colors = CardDefaults.cardColors(containerColor = color)) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            when (status) {
                is PairStatus.Working -> CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                is PairStatus.Failed -> Icon(Icons.Rounded.ErrorOutline, null)
                is PairStatus.Success -> Icon(Icons.Rounded.CheckCircle, null)
                PairStatus.Idle -> Unit
            }
            Text(text, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(start = 12.dp))
        }
    }
}
