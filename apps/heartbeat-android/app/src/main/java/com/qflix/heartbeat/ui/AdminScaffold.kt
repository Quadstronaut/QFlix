package com.qflix.heartbeat.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
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
                    title = { Text(selected.label) },
                    navigationIcon = {
                        IconButton(onClick = { scope.launch { drawerState.open() } }) {
                            Icon(Icons.Filled.Menu, contentDescription = "Open navigation drawer")
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
