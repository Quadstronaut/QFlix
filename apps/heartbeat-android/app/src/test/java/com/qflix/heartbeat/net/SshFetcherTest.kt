package com.qflix.heartbeat.net

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * No network in these tests — only the "not provisioned" path is exercised,
 * which [SshFetcher.exec] must short-circuit before it ever touches sshj.
 * That keeps this test running on the plain JVM (no Android device/emulator,
 * no BouncyCastle/AndroidConfig involved) while still proving the class
 * degrades to [Result.failure] instead of throwing.
 */
class SshFetcherTest {

    @get:Rule
    val tmp = TemporaryFolder()

    @Test
    fun `fetch returns failure, not a crash, when provision files are missing`() = runBlocking {
        val filesDir = tmp.newFolder("empty-files-dir")
        val fetcher = SshFetcher(filesDir)

        val result = fetcher.exec("status")

        assertTrue(result.isFailure)
        assertTrue(result.exceptionOrNull() is IllegalStateException)
    }

    @Test
    fun `fetch returns failure when only the config is present`() = runBlocking {
        val filesDir = tmp.newFolder("partial-files-dir")
        val dir = java.io.File(filesDir, "provision").apply { mkdirs() }
        java.io.File(dir, Provisioning.CONFIG_FILE_NAME).writeText(
            """{"host":"seedbox.example.com","port":22,"user":"quadstronaut"}""",
        )
        // phone_key and known_host still missing.
        val fetcher = SshFetcher(filesDir)

        val result = fetcher.exec("status")

        assertTrue(result.isFailure)
    }
}
