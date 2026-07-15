package com.qflix.heartbeat.net

import java.io.File
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * `filesDir/provision/config.json` shape written by `provision.ps1`.
 *
 * Deliberately minimal — just enough to open the SSH connection. The key
 * material and host-key pin live alongside it as sibling files
 * ([Provisioning.KEY_FILE_NAME], [Provisioning.KNOWN_HOST_FILE_NAME]), not in
 * this JSON, since sshj wants them as files/readers, not strings.
 */
@Serializable
data class ProvisionConfig(
    val host: String,
    val port: Int,
    val user: String,
)

/**
 * Locates and parses the provisioning bundle that `provision.ps1` copies into
 * app-private storage (`filesDir/provision/`): the SSH private key, the
 * pinned host-key line, and this JSON config. Nothing here touches the
 * network — [SshFetcher] is the only caller that does.
 *
 * Takes a plain [File] (the app's `filesDir`) rather than an Android
 * `Context` so it — and everything built on it — stays testable from a
 * plain JVM unit test.
 */
object Provisioning {
    private const val DIR_NAME = "provision"
    const val KEY_FILE_NAME = "phone_key"
    const val CONFIG_FILE_NAME = "config.json"
    const val KNOWN_HOST_FILE_NAME = "known_host"

    private val json = Json { ignoreUnknownKeys = true }

    fun provisionDir(filesDir: File): File = File(filesDir, DIR_NAME)

    fun keyFile(filesDir: File): File = File(provisionDir(filesDir), KEY_FILE_NAME)

    fun configFile(filesDir: File): File = File(provisionDir(filesDir), CONFIG_FILE_NAME)

    fun knownHostFile(filesDir: File): File = File(provisionDir(filesDir), KNOWN_HOST_FILE_NAME)

    /** True only when all three provisioning artifacts exist as regular files. */
    fun isProvisioned(filesDir: File): Boolean =
        keyFile(filesDir).isFile && configFile(filesDir).isFile && knownHostFile(filesDir).isFile

    /**
     * Parses `config.json`. Throws (IOException / SerializationException) if
     * the file is missing or malformed — callers should only invoke this
     * after [isProvisioned] returns true, or be ready to catch.
     */
    fun loadConfig(filesDir: File): ProvisionConfig =
        json.decodeFromString(ProvisionConfig.serializer(), configFile(filesDir).readText())
}
