package com.qflix.heartbeat.net

import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `filesDir/provision/routine.json`, written by `provision-fixer.ps1` (QFLX-70).
 *
 * [fireUrl] is the routine's `/fire` endpoint
 * (`https://api.anthropic.com/v1/claude_code/routines/trig_.../fire`) and
 * [token] its per-routine bearer token. Neither is baked into the APK: the
 * token can start a fully-autonomous cloud session with box access, so it
 * lives only in app-private storage, same as the SSH key bundle.
 */
@Serializable
data class RoutineConfig(
    val fireUrl: String,
    val token: String,
)

/** What a successful `/fire` returns: the new cloud session, ready to open. */
@Serializable
data class FireResult(
    @SerialName("claude_code_session_id") val sessionId: String,
    @SerialName("claude_code_session_url") val sessionUrl: String,
)

/**
 * Starts one Claude Code cloud session from the Fix page. A seam so
 * [com.qflix.heartbeat.ui.FixViewModel] tests run without the network.
 */
interface RoutineFirer {
    suspend fun fire(text: String): Result<FireResult>
}

/**
 * The wire contract for the routines `/fire` endpoint, kept as pure functions
 * so the exact headers/body/response shape are unit-tested on the JVM.
 * Endpoint is research-preview: if Anthropic bumps the beta header, change
 * [BETA] here and nowhere else.
 */
object RoutineFire {
    const val BETA = "experimental-cc-routine-2026-04-01"
    const val API_VERSION = "2023-06-01"

    /** Generous cap; a phone prompt is a paragraph, not a log dump. */
    const val MAX_TEXT_CHARS = 8_000

    private val json = Json { ignoreUnknownKeys = true }

    fun headers(token: String): Map<String, String> = mapOf(
        "Authorization" to "Bearer $token",
        "anthropic-beta" to BETA,
        "anthropic-version" to API_VERSION,
        "Content-Type" to "application/json",
    )

    /** Rejects blank/oversized prompts before they cost a fire (30/h cap). */
    fun body(text: String): String {
        val trimmed = text.trim()
        require(trimmed.isNotEmpty()) { "Prompt is empty." }
        require(trimmed.length <= MAX_TEXT_CHARS) { "Prompt over $MAX_TEXT_CHARS chars." }
        return buildJsonObject { put("text", trimmed) }.toString()
    }

    fun parse(body: String): FireResult = json.decodeFromString(FireResult.serializer(), body)
}

/** Production [RoutineFirer]: one HTTPS POST per fire, config from [filesDir]. */
class HttpRoutineFirer(private val filesDir: File) : RoutineFirer {

    override suspend fun fire(text: String): Result<FireResult> = withContext(Dispatchers.IO) {
        runCatching {
            check(Provisioning.isFixerProvisioned(filesDir)) {
                "Fix page not provisioned - run provision-fixer.ps1, then relaunch."
            }
            val config = Provisioning.loadRoutineConfig(filesDir)
            val payload = RoutineFire.body(text).toByteArray(Charsets.UTF_8)

            val conn = URL(config.fireUrl).openConnection() as HttpURLConnection
            try {
                conn.requestMethod = "POST"
                conn.connectTimeout = TIMEOUT_MS
                conn.readTimeout = TIMEOUT_MS
                conn.doOutput = true
                RoutineFire.headers(config.token).forEach { (k, v) -> conn.setRequestProperty(k, v) }
                conn.outputStream.use { it.write(payload) }

                val code = conn.responseCode
                // Error bodies come back on errorStream, not inputStream; read
                // whichever exists so a 401/429 surfaces its reason on screen.
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                check(code in 200..299) { "HTTP $code: ${text.take(300)}" }
                RoutineFire.parse(text)
            } finally {
                conn.disconnect()
            }
        }
    }

    private companion object {
        const val TIMEOUT_MS = 30_000
    }
}
