package com.qflix.heartbeat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import com.qflix.heartbeat.net.Provisioning
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

    // The SAME FQDN the SSH transport above already connects to, read from
    // the same provisioning bundle - never hardcoded, never a second secret.
    // StarrScreen's "Open in browser" button reuses it (Ultra.cc reverse-
    // proxies every app's web UI at https://<host>/<slug>/ off this one
    // host). Null when the device isn't provisioned yet; the button disables
    // itself rather than guessing a URL - see StarrScreen's kdoc.
    private val provisionedHost: String? by lazy {
        runCatching {
            if (Provisioning.isProvisioned(filesDir)) Provisioning.loadConfig(filesDir).host else null
        }.getOrNull()
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
                AdminScaffold(
                    viewModel = viewModel,
                    actionViewModel = actionViewModel,
                    transport = transport,
                    host = provisionedHost,
                )
            }
        }
    }
}
