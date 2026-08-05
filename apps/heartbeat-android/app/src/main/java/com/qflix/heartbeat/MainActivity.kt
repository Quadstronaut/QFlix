package com.qflix.heartbeat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import com.qflix.heartbeat.net.SshFetcher
import com.qflix.heartbeat.ui.ActionViewModel
import com.qflix.heartbeat.ui.AdminScaffold
import com.qflix.heartbeat.ui.StatusViewModel
import com.qflix.heartbeat.ui.theme.HeartbeatTheme
import java.security.Security
import org.bouncycastle.jce.provider.BouncyCastleProvider

class MainActivity : ComponentActivity() {

    // SshFetcher is the only production StatusTransport - filesDir is where
    // provision.ps1 (A3) drops the key bundle. Swapped for FakeTransport in
    // tests/previews via the StatusViewModel/ActionViewModel factory() seams.
    // One instance is shared across both ViewModels (and AppsScreen's own
    // app.list load, passed through AdminScaffold) - SshFetcher opens and
    // closes its own SSH session per exec() call, so it holds no state that
    // would need isolating per consumer.
    private val transport by lazy { SshFetcher(filesDir) }

    private val viewModel: StatusViewModel by viewModels {
        StatusViewModel.factory(transport)
    }

    private val actionViewModel: ActionViewModel by viewModels {
        ActionViewModel.factory(transport)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Swap in BouncyCastle ahead of Android's built-in provider so sshj
        // (A3) can rely on modern KEX/signature algorithms (ed25519 etc.) on
        // every API level we target, rather than whatever Conscrypt ships.
        Security.removeProvider("BC")
        Security.insertProviderAt(BouncyCastleProvider(), 1)

        enableEdgeToEdge()
        setContent {
            HeartbeatTheme {
                // Task 3: the drawer shell now owns top-level navigation;
                // it routes to DashboardScreen, the real AppsScreen (Task 5),
                // and (Task 6) stARR instead of this Activity going straight
                // to DashboardScreen itself.
                AdminScaffold(viewModel = viewModel, actionViewModel = actionViewModel, transport = transport)
            }
        }
    }
}
