package com.qflix.heartbeat.ui

import android.content.Intent
import android.net.Uri
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
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.qflix.heartbeat.model.Envelope
import com.qflix.heartbeat.model.StarrRow
import com.qflix.heartbeat.net.StatusTransport

/**
 * What the one-time `starr` fetch backing this screen can be showing.
 * Mirrors [AppsScreen]'s `AppListLoad` split for the same reason: kept
 * separate from [ActionState] so the row list doesn't disappear out from
 * under the screen when a row action (search-wanted) is fired, and firing
 * that action doesn't re-trigger this load.
 */
private sealed class StarrLoad {
    object Loading : StarrLoad()
    data class Ready(val rows: List<StarrRow>) : StarrLoad()
    data class Error(val message: String) : StarrLoad()
}

/**
 * The four-row *arr view: sonarr / sonarr2 / radarr / radarr2, painted from
 * ONE `starr` round trip (never a per-row fetch - the server fans out to all
 * four instances server-side precisely so the phone doesn't have to, over
 * what may be a flaky mobile link). A row whose `ok` is false still renders,
 * carrying its own [StarrRow.error] - see [StarrRow]'s kdoc - so one dead
 * *arr never blanks the other three off the page.
 *
 * Follows [AppsScreen]'s established shape rather than getting its own
 * ViewModel: neither brief specifies a `StarrViewModel`, the screen's only
 * load is a single one-shot fetch identical in kind to `app.list`'s, and row
 * actions already have a shared home in [actionViewModel] / [VerdictSheet].
 * Introducing a third state-management shape (a per-screen ViewModel) for a
 * screen with this little of its own state would be complexity the design
 * doesn't call for.
 *
 * [host] is the same FQDN [transport]'s SSH connection already uses (loaded
 * once from the app's provisioning bundle - see [MainActivity] - never
 * hardcoded). Per the design spec, Ultra.cc reverse-proxies every app at
 * `https://<host>/<slug>/`; "Open in browser" reuses that same convention
 * with no credential replay, matching the spec's explicit rejection of
 * *arr autologin. `null` (provisioning unreadable) disables the button
 * rather than guessing a URL.
 */
@Composable
fun StarrScreen(transport: StatusTransport, actionViewModel: ActionViewModel, host: String?) {
    var loadState by remember { mutableStateOf<StarrLoad>(StarrLoad.Loading) }
    var reloadKey by remember { mutableStateOf(0) }

    LaunchedEffect(reloadKey) {
        loadState = StarrLoad.Loading
        transport.exec("starr")
            .mapCatching { body -> Envelope.parse(body).getOrThrow() }
            .fold(
                onSuccess = { envelope ->
                    loadState = if (envelope.ok) {
                        StarrLoad.Ready(StarrRow.parseAll(envelope))
                    } else {
                        StarrLoad.Error(envelope.verdict)
                    }
                },
                onFailure = { e -> loadState = StarrLoad.Error(e.message ?: "Unknown error") },
            )
    }

    val actionState by actionViewModel.state.collectAsState()

    when (val s = loadState) {
        StarrLoad.Loading -> LoadingBody()
        is StarrLoad.Error -> ErrorBody(s.message) { reloadKey++ }
        is StarrLoad.Ready -> StarrList(
            rows = s.rows,
            host = host,
            onSearchWanted = { slug -> actionViewModel.fire("arr.search_wanted $slug") },
        )
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
private fun StarrList(rows: List<StarrRow>, host: String?, onSearchWanted: (slug: String) -> Unit) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(rows) { row ->
            StarrRowCard(row, host, onSearchWanted)
        }
    }
}

@Composable
private fun StarrRowCard(row: StarrRow, host: String?, onSearchWanted: (slug: String) -> Unit) {
    val context = LocalContext.current

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(row.slug, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Text(
                    row.kind,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Spacer(Modifier.height(4.dp))
            // "12/30 complete" - coarse presence/completion counts only, per
            // the privacy constraint; never a per-title breakdown here.
            Text(
                "${row.completeCount}/${row.titleCount} complete",
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                row.human,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (!row.ok) {
                Spacer(Modifier.height(4.dp))
                Text(
                    "degraded: ${row.error}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                // Disabled when this same round trip already reported the
                // instance degraded - firing a search against an *arr we
                // already know is down would only reproduce a refusal the
                // page already has the answer to.
                TextButton(onClick = { onSearchWanted(row.slug) }, enabled = row.ok) {
                    Text("Search all wanted")
                }
                TextButton(
                    onClick = {
                        host?.let {
                            val url = "https://$it/${row.slug}/"
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                        }
                    },
                    enabled = host != null,
                ) {
                    Text("Open in browser")
                }
            }
        }
    }
}
