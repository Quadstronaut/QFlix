package com.qflix.heartbeat.net

import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RoutineFireTest {

    @get:Rule
    val tmp = TemporaryFolder()

    @Test
    fun `headers carry bearer token and the routine beta`() {
        val h = RoutineFire.headers("tok123")
        assertEquals("Bearer tok123", h["Authorization"])
        assertEquals("experimental-cc-routine-2026-04-01", h["anthropic-beta"])
        assertEquals("2023-06-01", h["anthropic-version"])
        assertEquals("application/json", h["Content-Type"])
    }

    @Test
    fun `body is a json text field, trimmed, with quotes escaped`() {
        val body = RoutineFire.body("  sonarr \"red\"\nfix it  ")
        val text = Json.parseToJsonElement(body).jsonObject["text"]!!.jsonPrimitive.content
        assertEquals("sonarr \"red\"\nfix it", text)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `blank prompt is refused before it costs a fire`() {
        RoutineFire.body("   \n ")
    }

    @Test(expected = IllegalArgumentException::class)
    fun `oversized prompt is refused`() {
        RoutineFire.body("x".repeat(RoutineFire.MAX_TEXT_CHARS + 1))
    }

    @Test
    fun `parse reads the documented fire response and ignores extras`() {
        val r = RoutineFire.parse(
            """{"type":"routine_fire","claude_code_session_id":"session_01ABC",
               "claude_code_session_url":"https://claude.ai/code/session_01ABC"}""",
        )
        assertEquals("session_01ABC", r.sessionId)
        assertEquals("https://claude.ai/code/session_01ABC", r.sessionUrl)
    }

    @Test
    fun `fixer provisioning is independent of the ssh bundle`() {
        val filesDir = tmp.newFolder("files")
        assertFalse(Provisioning.isFixerProvisioned(filesDir))
        File(filesDir, "provision").mkdirs()
        Provisioning.routineFile(filesDir).writeText(
            """{"fireUrl":"https://api.anthropic.com/v1/claude_code/routines/trig_x/fire","token":"t"}""",
        )
        assertTrue(Provisioning.isFixerProvisioned(filesDir))
        assertFalse(Provisioning.isProvisioned(filesDir))
        assertEquals("t", Provisioning.loadRoutineConfig(filesDir).token)
    }
}
