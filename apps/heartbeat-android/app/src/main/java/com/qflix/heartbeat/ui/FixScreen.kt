package com.qflix.heartbeat.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * QFLX-70 Fix page: type what's wrong, tap Send, a Claude cloud session starts
 * with the QFlix steering (caveman, guards, autonomy) baked into the routine's
 * saved prompt. "Open session" hands the claude.ai/code URL to the system,
 * which routes it to the Claude app when installed - that's where the operator
 * steers and checks in. Nothing here talks to the box.
 */
@Composable
fun FixScreen(viewModel: FixViewModel) {
    val state by viewModel.state.collectAsState()
    val entries by viewModel.entries.collectAsState()
    val uri = LocalUriHandler.current
    var prompt by rememberSaveable { mutableStateOf("") }
    val sending = state is FixState.Sending

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            OutlinedTextField(
                value = prompt,
                onValueChange = { prompt = it },
                label = { Text("What's wrong?") },
                placeholder = { Text("e.g. Sonarr red in Kuma since 9am, fix it") },
                minLines = 5,
                enabled = !sending,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Button(
                    onClick = { viewModel.send(prompt) },
                    enabled = !sending && prompt.isNotBlank(),
                ) { Text("Send to Claude") }
                if (sending) {
                    CircularProgressIndicator(Modifier.padding(start = 16.dp))
                }
            }
        }
        item {
            when (val s = state) {
                is FixState.Fired -> Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Session started. Steer it in the Claude app.")
                        TextButton(onClick = { uri.openUri(s.sessionUrl) }) { Text("Open session") }
                        TextButton(onClick = { prompt = ""; viewModel.reset() }) { Text("New prompt") }
                    }
                }
                is FixState.Failed -> Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Not sent: ${s.message}", color = MaterialTheme.colorScheme.error)
                        TextButton(onClick = { viewModel.reset() }) { Text("Dismiss") }
                    }
                }
                else -> Unit
            }
        }
        if (entries.isNotEmpty()) {
            item { Text("Recent sessions", style = MaterialTheme.typography.titleSmall) }
        }
        items(entries, key = { it.firedAtEpochMs }) { entry ->
            Card(Modifier.fillMaxWidth()) {
                Row(
                    Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(STAMP.format(Date(entry.firedAtEpochMs)), style = MaterialTheme.typography.labelSmall)
                        Text(entry.prompt, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    }
                    TextButton(onClick = { uri.openUri(entry.sessionUrl) }) { Text("Open") }
                }
            }
        }
    }
}

private val STAMP = SimpleDateFormat("MMM d HH:mm", Locale.US)
