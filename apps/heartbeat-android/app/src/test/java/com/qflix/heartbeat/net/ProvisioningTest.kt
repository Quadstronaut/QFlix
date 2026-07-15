package com.qflix.heartbeat.net

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * Exercises [Provisioning] against a throwaway `filesDir` — no Android
 * Context, no device, so this runs as a plain JVM unit test.
 */
class ProvisioningTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private fun provisionDir(filesDir: File): File =
        File(filesDir, "provision").apply { mkdirs() }

    @Test
    fun `isProvisioned is false when the provision directory is empty`() {
        val filesDir = tmp.newFolder("files")

        assertFalse(Provisioning.isProvisioned(filesDir))
    }

    @Test
    fun `isProvisioned is false when only some of the three files exist`() {
        val filesDir = tmp.newFolder("files-partial")
        val dir = provisionDir(filesDir)
        File(dir, Provisioning.CONFIG_FILE_NAME).writeText("{}")
        // phone_key and known_host deliberately missing.

        assertFalse(Provisioning.isProvisioned(filesDir))
    }

    @Test
    fun `isProvisioned is true once key, config and known_host all exist`() {
        val filesDir = tmp.newFolder("files-complete")
        val dir = provisionDir(filesDir)
        File(dir, Provisioning.KEY_FILE_NAME).writeText("fake-key-bytes")
        File(dir, Provisioning.CONFIG_FILE_NAME).writeText(
            """{"host":"seedbox.example.com","port":22,"user":"quadstronaut"}""",
        )
        File(dir, Provisioning.KNOWN_HOST_FILE_NAME).writeText(
            "seedbox.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample",
        )

        assertTrue(Provisioning.isProvisioned(filesDir))
    }

    @Test
    fun `loadConfig parses the config json shape from the plan contract`() {
        val filesDir = tmp.newFolder("files-config")
        val dir = provisionDir(filesDir)
        File(dir, Provisioning.CONFIG_FILE_NAME).writeText(
            """{"host":"seedbox.example.com","port":22,"user":"quadstronaut"}""",
        )

        val config = Provisioning.loadConfig(filesDir)

        assertEquals(ProvisionConfig(host = "seedbox.example.com", port = 22, user = "quadstronaut"), config)
    }

    @Test
    fun `loadConfig ignores unknown extra fields instead of failing`() {
        val filesDir = tmp.newFolder("files-config-extra")
        val dir = provisionDir(filesDir)
        File(dir, Provisioning.CONFIG_FILE_NAME).writeText(
            """{"host":"seedbox.example.com","port":22,"user":"quadstronaut","note":"unused"}""",
        )

        val config = Provisioning.loadConfig(filesDir)

        assertEquals("seedbox.example.com", config.host)
        assertEquals(22, config.port)
        assertEquals("quadstronaut", config.user)
    }
}
