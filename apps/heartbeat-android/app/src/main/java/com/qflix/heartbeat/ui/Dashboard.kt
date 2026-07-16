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
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.qflix.heartbeat.model.AlertView
import com.qflix.heartbeat.model.DashboardState
import com.qflix.heartbeat.model.DownloadsView
import com.qflix.heartbeat.model.KumaView
import com.qflix.heartbeat.model.QuotaView
import com.qflix.heartbeat.model.SectionState
import com.qflix.heartbeat.model.StreamsView
import com.qflix.heartbeat.model.Top5View
import com.qflix.heartbeat.net.StatusTransport
import com.qflix.heartbeat.ui.theme.HeartbeatTheme

/**
 * Root screen. Hosts the top bar (data-age + manual refresh), the
 * pull-to-refresh gesture, and the section list - in the order the design
 * spec fixes: alerts, quota, kuma, streams + top5s, downloads.
 *
 * A [StatusUiState.Error] (including "not provisioned") replaces the whole
 * screen with a centered message and a retry button rather than trying to
 * render an empty dashboard - there is nothing useful to show underneath it.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(viewModel: StatusViewModel) {
    val state by viewModel.uiState.collectAsState()
    val isRefreshing by viewModel.isRefreshing.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("QFlix Heartbeat")
                        // Data age comes pre-formatted on DashboardState itself
                        // (A2's ViewState.from already computed it against the
                        // fetch time) - no re-derivation here.
                        (state as? StatusUiState.Ready)?.let { ready ->
                            Text(
                                text = ready.dashboard.dataAge,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                },
                actions = {
                    IconButton(onClick = { viewModel.refresh() }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
                    }
                },
            )
        },
    ) { innerPadding ->
        when (val s = state) {
            is StatusUiState.Loading -> LoadingScreen(Modifier.padding(innerPadding))
            is StatusUiState.Error -> ErrorScreen(s.message, s.retry, Modifier.padding(innerPadding))
            is StatusUiState.Ready -> PullToRefreshBox(
                isRefreshing = isRefreshing,
                onRefresh = { viewModel.refresh() },
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding),
            ) {
                DashboardBody(s.dashboard)
            }
        }
    }
}

@Composable
private fun LoadingScreen(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text("Loading…", style = MaterialTheme.typography.bodyLarge)
    }
}

/**
 * Full-screen error state. Covers every [StatusTransport] failure mode
 * uniformly, including "device not provisioned" - [SshFetcher] phrases that
 * message as "Device not provisioned — run provision.ps1, then relaunch the
 * app.", which renders here verbatim, satisfying the plan's "Not provisioned
 * - run provision.ps1" requirement without a separate code path.
 */
@Composable
private fun ErrorScreen(message: String, retry: () -> Unit, modifier: Modifier = Modifier) {
    Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier.padding(24.dp),
        ) {
            Text(
                text = message,
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.error,
            )
            Spacer(Modifier.height(16.dp))
            AssistChip(onClick = retry, label = { Text("Retry") })
        }
    }
}

@Composable
private fun DashboardBody(dashboard: DashboardState) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { AlertBanner(dashboard.alerts) }
        item { QuotaBars(dashboard.quota) }
        item { KumaCard(dashboard.kuma) }
        item { StreamsCard(dashboard.streams) }
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Top5RequestsCard(dashboard.top5, Modifier.weight(1f))
                Top5WatchCard(dashboard.top5, Modifier.weight(1f))
            }
        }
        item { DownloadsCard(dashboard.downloads) }
    }
}

// ---- 1. Alert banner ----

@Composable
fun AlertBanner(alerts: List<AlertView>) {
    Card(
        colors = CardDefaults.cardColors(
            containerColor = if (alerts.isEmpty()) {
                Color(0xFF1B3A2A)
            } else {
                Color(0xFF3A1B1B)
            },
        ),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            if (alerts.isEmpty()) {
                Text(
                    "All clear",
                    style = MaterialTheme.typography.titleMedium,
                    color = Color(0xFF81C784),
                    fontWeight = FontWeight.Bold,
                )
            } else {
                alerts.forEach { alert ->
                    Text(
                        text = alert.text,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (alert.level == "crit") Color(0xFFEF9A9A) else Color(0xFFFFCC80),
                    )
                }
            }
        }
    }
}

// ---- shared section chrome ----

/** Title row every card shares, plus the inline error chip [SectionState.SectionError] calls for. */
@Composable
private fun SectionHeader(title: String, state: SectionState<*>) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        when (state) {
            is SectionState.SectionError -> ErrorChip(state.message)
            SectionState.Missing -> ErrorChip("no data")
            is SectionState.Ok -> {}
        }
    }
}

@Composable
private fun ErrorChip(message: String) {
    AssistChip(
        onClick = {},
        label = { Text(message, style = MaterialTheme.typography.labelSmall) },
        colors = AssistChipDefaults.assistChipColors(
            labelColor = MaterialTheme.colorScheme.error,
        ),
    )
}

// ---- 2. Quota bars ----

@Composable
fun QuotaBars(state: SectionState<QuotaView>) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            SectionHeader("Quota", state)
            if (state is SectionState.Ok) {
                val quota = state.data
                QuotaBar(label = "Disk", valueLabel = quota.diskLabel, pct = quota.diskPct)
                QuotaBar(label = "Bandwidth", valueLabel = quota.bandwidthLabel, pct = quota.bandwidthUsedPct)
            }
        }
    }
}

@Composable
private fun QuotaBar(label: String, valueLabel: String, pct: Double) {
    Column {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, style = MaterialTheme.typography.bodyMedium)
            Text(valueLabel, style = MaterialTheme.typography.bodyMedium)
        }
        Spacer(Modifier.height(4.dp))
        LinearProgressIndicator(
            progress = { (pct / 100.0).coerceIn(0.0, 1.0).toFloat() },
            modifier = Modifier
                .fillMaxWidth()
                .height(8.dp),
            color = quotaColor(pct),
        )
    }
}

/** Same thresholds for disk and bandwidth: both bars are "how close to the limit", higher = worse. */
@Composable
private fun quotaColor(pct: Double): Color = when {
    pct >= 90.0 -> MaterialTheme.colorScheme.error
    pct >= 80.0 -> Color(0xFFFFA726)
    else -> MaterialTheme.colorScheme.primary
}

// ---- 3. Kuma ----

@Composable
fun KumaCard(state: SectionState<KumaView>) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            SectionHeader("Kuma", state)
            if (state is SectionState.Ok) {
                val kuma = state.data
                Text(kuma.summary, style = MaterialTheme.typography.headlineSmall)
                kuma.red.forEach { red ->
                    Column(Modifier.padding(top = 4.dp)) {
                        Text(red.name, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Bold)
                        Text(
                            red.msg,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Text(
                            "since ${red.since}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
    }
}

// ---- 4. Streams + top5s ----

@Composable
fun StreamsCard(state: SectionState<StreamsView>) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            SectionHeader("Streams", state)
            if (state is SectionState.Ok) {
                val streams = state.data
                Text(
                    streams.fraction,
                    style = MaterialTheme.typography.displaySmall.copy(fontSize = 48.sp),
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    "${streams.transcodes} transcode(s) - ${streams.wanKbps} kbps WAN",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
fun Top5RequestsCard(state: SectionState<Top5View>, modifier: Modifier = Modifier) {
    Card(modifier = modifier) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            SectionHeader("Requests (30d)", state)
            if (state is SectionState.Ok) {
                state.data.requests.forEach { entry ->
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(
                            entry.user,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.weight(1f, fill = true),
                        )
                        Text("${entry.count}", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
    }
}

@Composable
fun Top5WatchCard(state: SectionState<Top5View>, modifier: Modifier = Modifier) {
    Card(modifier = modifier) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            SectionHeader("Watch time (30d)", state)
            if (state is SectionState.Ok) {
                state.data.watch.forEach { entry ->
                    Column(Modifier.padding(vertical = 2.dp)) {
                        Text(entry.user, style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.Bold)
                        Text(
                            "${entry.hoursLabel} - ${entry.plays} plays",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
    }
}

// ---- 5. Downloads ----

@Composable
fun DownloadsCard(state: SectionState<DownloadsView>) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            SectionHeader("Downloads", state)
            if (state is SectionState.Ok) {
                val downloads = state.data
                Text(downloads.qbitSummary, style = MaterialTheme.typography.bodyMedium)
                Text(downloads.sabSummary, style = MaterialTheme.typography.bodyMedium)

                if (downloads.stuck.isNotEmpty()) {
                    Text(
                        "Stuck",
                        style = MaterialTheme.typography.labelLarge,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                    downloads.stuck.forEach { stuck ->
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text("${stuck.hash8} - ${stuck.rule}", style = MaterialTheme.typography.bodySmall)
                            Text(
                                if (stuck.acted) "${stuck.hoursLabel} (acted)" else stuck.hoursLabel,
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }

                if (downloads.recentUnsticks.isNotEmpty()) {
                    Text(
                        "Recent unsticks",
                        style = MaterialTheme.typography.labelLarge,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                    downloads.recentUnsticks.forEach { unstick ->
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text(unstick.hash8, style = MaterialTheme.typography.bodySmall)
                            Text(unstick.result, style = MaterialTheme.typography.bodySmall)
                        }
                    }
                }
            }
        }
    }
}

// ---- previews: render fixture JSON through the real VM, no device or test sourceset needed ----

/** Main-sourceset preview double - the shared [FakeTransport] lives under src/test and isn't visible here. */
private class PreviewTransport(private val result: Result<String>) : StatusTransport {
    override suspend fun fetch(): Result<String> = result
}

@Preview(showBackground = true, heightDp = 1400)
@Composable
private fun DashboardScreenPreview() {
    val fixture = """
        {"meta": {"generated_at": "2026-07-15T23:48:07Z", "version": 1},
         "quota": {"ok": true, "disk": {"used_gb": 2074, "total_gb": 2794, "pct": 74.2},
                   "bandwidth": {"used_pct": 3.49, "next_reset": "2026-07-28T00:00:00"}},
         "kuma": {"ok": true, "total": 55, "up": 51, "down": 4,
                  "red": [{"name": "QFlix Reaper", "msg": "No heartbeat in the time window", "since": "2026-07-15 13:17:02"}]},
         "streams": {"ok": true, "streams": 1, "users": 1, "transcodes": 1, "wan_kbps": 14994},
         "top5": {"ok": true,
                  "requests_30d": [{"user": "sarahvanpelt", "count": 12}],
                  "watch_30d": [{"user": "BAsylum", "hours": 79.1, "plays": 106}]},
         "downloads": {"ok": true, "qbit": {"total": 12, "active": 1, "seeding": 10},
                       "sab": {"queued": 1, "mb_left": 0, "mb_total": 0},
                       "stuck": [{"hash8": "cbed175c", "hours": 3, "rule": "stalledDL", "acted": true}],
                       "recent_unsticks": [{"hash8": "f0a3658d", "result": "qbit-orphan-removed"}]},
         "alerts": [{"level": "crit", "text": "Kuma down: QFlix Reaper"}]}
    """.trimIndent()

    HeartbeatTheme {
        DashboardScreen(viewModel = viewModel(factory = StatusViewModel.factory(PreviewTransport(Result.success(fixture)))))
    }
}

@Preview(showBackground = true)
@Composable
private fun DashboardScreenNotProvisionedPreview() {
    HeartbeatTheme {
        DashboardScreen(
            viewModel = viewModel(
                factory = StatusViewModel.factory(
                    PreviewTransport(
                        Result.failure(
                            IllegalStateException("Device not provisioned — run provision.ps1, then relaunch the app."),
                        ),
                    ),
                ),
            ),
        )
    }
}
