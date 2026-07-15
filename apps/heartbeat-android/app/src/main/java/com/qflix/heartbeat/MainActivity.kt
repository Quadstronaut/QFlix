package com.qflix.heartbeat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.tooling.preview.Preview
import com.qflix.heartbeat.ui.theme.HeartbeatTheme
import java.security.Security
import org.bouncycastle.jce.provider.BouncyCastleProvider

class MainActivity : ComponentActivity() {
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
                PlaceholderScreen()
            }
        }
    }
}

/** A2+ replaces this with the real dashboard fed by [StatusViewModel]. */
@Composable
fun PlaceholderScreen() {
    Scaffold { innerPadding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = "QFlix Heartbeat",
                style = MaterialTheme.typography.headlineMedium,
            )
        }
    }
}

@Preview(showBackground = true)
@Composable
private fun PlaceholderScreenPreview() {
    HeartbeatTheme {
        PlaceholderScreen()
    }
}
