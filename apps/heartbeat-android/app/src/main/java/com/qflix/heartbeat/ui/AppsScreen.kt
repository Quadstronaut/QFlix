package com.qflix.heartbeat.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.qflix.heartbeat.model.AppRow
import com.qflix.heartbeat.model.Envelope
import com.qflix.heartbeat.net.StatusTransport

/**
 * What the one-time `app.list` fetch backing this screen can be showing.
 * Deliberately separate from [ActionState]: that state machine is reserved
 * for user-fired row actions (start/stop/restart) and their [VerdictSheet],
 * so listing the apps doesn't pop the verdict sheet on every screen visit
 * and firing "restart sonarr" doesn't blank the row list out from under it.
 */
private sealed class AppListLoad {
    object Loading : AppListLoad()
    data class Ready(val rows: List<AppRow>) : AppListLoad()
    data class Error(val message: String) : AppListLoad()
}

/**
 * The lifecycle list: one row per app `app.list` reports (18 `ucc` + 6
 * `systemd` per the design spec), each showing its slug, its class as a
 * visible badge - the operator must be able to see whether an app is driven
 * through Ultra's approved command layer or systemd directly, not just
 * infer it - and start/stop/restart actions.
 *
 * Replaces the Task 3 placeholder. The row list is loaded directly through
 * [transport] (independent of [actionViewModel]); each row's action fires
 * `"app.<action> <slug>"` through [actionViewModel] and its result surfaces
 * in the shared [VerdictSheet].
 */
@Composable
fun AppsScreen(transport: StatusTransport, actionViewModel: ActionViewModel) {
    var loadState by remember { mutableStateOf<AppListLoad>(AppListLoad.Loading) }
    var reloadKey by remember { mutableStateOf(0) }

    LaunchedEffect(reloadKey) {
        loadState = AppListLoad.Loading
        transport.exec("app.list")
            .mapCatching { body -> Envelope.parse(body).getOrThrow() }
            .fold(
                onSuccess = { envelope ->
                    loadState = if (envelope.ok) {
                        AppListLoad.Ready(AppRow.parseList(envelope))
                    } else {
                        AppListLoad.Error(envelope.verdict)
                    }
                },
                onFailure = { e -> loadState = AppListLoad.Error(e.message ?: "Unknown error") },
            )
    }

    val actionState by actionViewModel.state.collectAsState()

    when (val s = loadState) {
        AppListLoad.Loading -> LoadingBody()
        is AppListLoad.Error -> ErrorBody(s.message) { reloadKey++ }
        is AppListLoad.Ready -> AppList(s.rows) { action, slug ->
            actionViewModel.fire("app.$action $slug")
        }
    }

    VerdictSheet(state = actionState, onDismiss = { actionViewModel.reset() })
}

@Composable
private fun LoadingBody() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        CircularProgressIndicator()
    }
}

@Composable
private fun ErrorBody(message: String, retry: () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(24.dp)) {
            Text(message, style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.error)
            Spacer(Modifier.height(16.dp))
            AssistChip(onClick = retry, label = { Text("Retry") })
        }
    }
}

@Composable
private fun AppList(rows: List<AppRow>, onAction: (action: String, slug: String) -> Unit) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(rows) { row ->
            AppRowCard(row) { action -> onAction(action, row.slug) }
        }
    }
}

@Composable
private fun AppRowCard(row: AppRow, onAction: (action: String) -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(row.slug, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                ClassBadge(row.klass)
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { onAction("start") }) { Text("Start") }
                TextButton(onClick = { onAction("stop") }) { Text("Stop") }
                TextButton(onClick = { onAction("restart") }) { Text("Restart") }
            }
        }
    }
}

/**
 * The visible UCC/systemd distinction the design spec requires: a "ucc" row
 * badges as Ultra-managed (primary container), everything else - "systemd"
 * as `app.list` reports it - badges neutrally. Never inferred from slug
 * name; always taken from the class the server reports.
 */
@Composable
private fun ClassBadge(klass: String) {
    val isUcc = klass == "ucc"
    Surface(
        color = if (isUcc) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.secondaryContainer,
        shape = RoundedCornerShape(6.dp),
    ) {
        Text(
            text = if (isUcc) "UCC" else klass,
            style = MaterialTheme.typography.labelSmall,
            color = if (isUcc) MaterialTheme.colorScheme.onPrimaryContainer else MaterialTheme.colorScheme.onSecondaryContainer,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
        )
    }
}
