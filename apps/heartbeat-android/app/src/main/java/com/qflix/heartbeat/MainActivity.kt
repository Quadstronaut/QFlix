package com.qflix.heartbeat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import com.qflix.heartbeat.net.SshFetcher
import com.qflix.heartbeat.ui.DashboardScreen
import com.qflix.heartbeat.ui.StatusViewModel
import com.qflix.heartbeat.ui.theme.HeartbeatTheme
import java.security.Security
import org.bouncycastle.jce.provider.BouncyCastleProvider

class MainActivity : ComponentActivity() {

    // SshFetcher is the only production StatusTransport - filesDir is where
    // provision.ps1 (A3) drops the key bundle. Swapped for FakeTransport in
    // tests/previews via the same StatusViewModel.factory() seam.
    private val viewModel: StatusViewModel by viewModels {
        StatusViewModel.factory(SshFetcher(filesDir))
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
                DashboardScreen(viewModel = viewModel)
            }
        }
    }
}
