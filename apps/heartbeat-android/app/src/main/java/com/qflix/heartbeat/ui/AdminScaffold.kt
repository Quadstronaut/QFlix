package com.qflix.heartbeat.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.qflix.heartbeat.net.StatusTransport
import kotlinx.coroutines.launch

/**
 * The three top-level pages QFlix Admin routes between. DASHBOARD is listed
 * first (and [entries]/[values] preserve declaration order) so the drawer's
 * default landing spot - and existing Heartbeat users' muscle memory - is
 * unchanged by the rename in Task 2.
 */
enum class Destination(val label: String) {
    DASHBOARD("Dashboard"),
    APPS("Apps"),
    STARR("stARR"),
}

/**
 * Outer shell for the whole app: a Material 3 [ModalNavigationDrawer] wrapping
 * a [Scaffold] whose [TopAppBar] navigation icon opens it. Swapping the body
 * for [AppsScreen] / [StarrScreen] is a pure state change here - no
 * navigation library, no back stack - because there are only three
 * destinations and no deep linking between them (per the design spec).
 *
 * The selected [Destination] lives in [rememberSaveable] rather than plain
 * [androidx.compose.runtime.mutableStateOf] so a rotation (or process death
 * from Android reclaiming memory) reopens on the same page instead of
 * silently bouncing the operator back to Dashboard.
 *
 * [AppsScreen] is Task 5's real 24-row lifecycle list. [StarrScreen] (Task 6)
 * is the real 4-row *arr view; [host] is threaded through to it purely for
 * its "Open in browser" button (see that screen's kdoc) - nothing else here
 * needs it.
 *
 * The one [TopAppBar] here is the ONLY app bar in the tree (Task 7): Dashboard
 * (Heartbeat v2) used to carry its own `Scaffold`+`TopAppBar` with a refresh
 * icon and a data-age subtitle, which stacked a second bar underneath this
 * one the moment the drawer landed in Task 3. `DashboardScreen` now renders
 * body content only; its refresh action and data-age subtitle are folded in
 * here instead, shown only while [Destination.DASHBOARD] is selected -
 * [viewModel]'s state is read directly for that, the same [StatusViewModel]
 * instance [DashboardScreen] itself collects from.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AdminScaffold(
    viewModel: StatusViewModel,
    actionViewModel: ActionViewModel,
    transport: StatusTransport,
    host: String? = null,
) {
    val drawerState = rememberDrawerState(initialValue = DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    var selected by rememberSaveable { mutableStateOf(Destination.DASHBOARD) }

    val dashboardState by viewModel.uiState.collectAsState()
    val dashboardRefreshing by viewModel.isRefreshing.collectAsState()

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ModalDrawerSheet {
                Destination.entries.forEach { destination ->
                    NavigationDrawerItem(
                        label = { Text(destination.label) },
                        selected = destination == selected,
                        onClick = {
                            selected = destination
                            scope.launch { drawerState.close() }
                        },
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                    )
                }
            }
        },
    ) {
        Scaffold(
            topBar = {
                TopAppBar(
                    title = {
                        Column {
                            Text(selected.label)
                            if (selected == Destination.DASHBOARD) {
                                // Pre-formatted by ViewState.from already (see
                                // DashboardState.dataAge) - no re-derivation here.
                                (dashboardState as? StatusUiState.Ready)?.let { ready ->
                                    Text(
                                        text = ready.dashboard.dataAge,
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            }
                        }
                    },
                    navigationIcon = {
                        IconButton(onClick = { scope.launch { drawerState.open() } }) {
                            Icon(Icons.Filled.Menu, contentDescription = "Open navigation drawer")
                        }
                    },
                    actions = {
                        if (selected == Destination.DASHBOARD) {
                            // Disabled while a fetch is already in flight - belt-
                            // and-suspenders alongside StatusViewModel's own
                            // re-entrancy guard, which is what actually prevents
                            // a second concurrent transport.exec() if this slips
                            // through.
                            IconButton(onClick = { viewModel.refresh() }, enabled = !dashboardRefreshing) {
                                Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
                            }
                        }
                    },
                )
            },
        ) { innerPadding ->
            Box(Modifier.padding(innerPadding)) {
                when (selected) {
                    Destination.DASHBOARD -> DashboardScreen(viewModel = viewModel)
                    Destination.APPS -> AppsScreen(transport = transport, actionViewModel = actionViewModel)
                    Destination.STARR -> StarrScreen(
                        transport = transport,
                        actionViewModel = actionViewModel,
                        host = host,
                    )
                }
            }
        }
    }
}
