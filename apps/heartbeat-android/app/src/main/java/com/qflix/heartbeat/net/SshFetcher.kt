package com.qflix.heartbeat.net

import java.io.File
import java.security.Security
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import net.schmizz.sshj.AndroidConfig
import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.common.IOUtils
import net.schmizz.sshj.transport.verification.OpenSSHKnownHosts
import org.bouncycastle.jce.provider.BouncyCastleProvider

/**
 * [StatusTransport] backed by a real SSH connection to the seedbox.
 *
 * Every credential comes from the provisioning bundle in app-private storage
 * (see [Provisioning]) — nothing is baked into the APK. The server's forced
 * command now routes on the exec string instead of ignoring it, so [exec]
 * threads the requested verb all the way to the wire.
 *
 * Host key checking is a strict pin, not trust-on-first-use: [knownHostFile]
 * holds exactly one `ssh-keyscan`-captured line, and sshj's
 * [OpenSSHKnownHosts] rejects any host that doesn't match it — no prompt, no
 * fallback.
 */
class SshFetcher(private val filesDir: File) : StatusTransport {

    override suspend fun exec(verb: String): Result<String> = withContext(Dispatchers.IO) {
        if (!Provisioning.isProvisioned(filesDir)) {
            return@withContext Result.failure(
                IllegalStateException(
                    "Device not provisioned — run provision.ps1, then relaunch the app.",
                ),
            )
        }

        runCatching {
            val config = Provisioning.loadConfig(filesDir)
            ensureBouncyCastleProvider()
            connectAndFetch(config, verb)
        }
    }

    /** Blocking; must run off the main thread (see [Dispatchers.IO] above). */
    private fun connectAndFetch(config: ProvisionConfig, verb: String): String {
        // AndroidConfig (not DefaultConfig) is required on Android per the
        // sshj FAQ — it swaps in key/algorithm defaults that work against
        // Android's stock provider stack instead of a desktop JVM's.
        val ssh = SSHClient(AndroidConfig())
        ssh.connectTimeout = TIMEOUT_MS
        ssh.timeout = TIMEOUT_MS
        ssh.addHostKeyVerifier(OpenSSHKnownHosts(Provisioning.knownHostFile(filesDir)))

        return try {
            ssh.connect(config.host, config.port)
            val keys = ssh.loadKeys(Provisioning.keyFile(filesDir).absolutePath)
            ssh.authPublickey(config.user, keys)

            val session = ssh.startSession()
            try {
                // The forced command routes on this; before the dispatcher
                // landed it was ignored, so this string was decorative.
                val cmd = session.exec(verb)
                val output = IOUtils.readFully(cmd.inputStream).toString()
                cmd.join(TIMEOUT_MS.toLong(), TimeUnit.MILLISECONDS)
                output
            } finally {
                session.close()
            }
        } finally {
            ssh.disconnect()
        }
    }

    companion object {
        private const val TIMEOUT_MS = 15_000

        @Volatile
        private var bcRegistered = false

        /**
         * Registers BouncyCastle ahead of Android's built-in provider, once,
         * lazily, immediately before the first SSH connection attempt — sshj
         * needs it for ed25519 KEX/signature support that Android's stock
         * provider stack doesn't reliably offer. Safe to call repeatedly.
         */
        @Synchronized
        private fun ensureBouncyCastleProvider() {
            if (bcRegistered) return
            Security.removeProvider("BC")
            Security.insertProviderAt(BouncyCastleProvider(), 1)
            bcRegistered = true
        }
    }
}
