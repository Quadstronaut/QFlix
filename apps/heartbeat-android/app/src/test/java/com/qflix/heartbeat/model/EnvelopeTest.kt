package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EnvelopeTest {

    @Test
    fun `parses a success envelope`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"app.restart","target":"sonarr",
                "verdict":"restart sonarr","lines":["a","b"],"elapsed_s":4.2}"""
        ).getOrThrow()
        assertTrue(e.ok)
        assertEquals("app.restart", e.verb)
        assertEquals("sonarr", e.target)
        assertEquals(listOf("a", "b"), e.lines)
        assertEquals(4.2, e.elapsedS, 0.001)
    }

    @Test
    fun `a failure envelope is data, not an error`() {
        // ok=false is a normal server answer - the app renders the verdict.
        val e = Envelope.parse(
            """{"ok":false,"verb":"logs","target":"listmonk",
                "verdict":"listmonk logs are not exposed over this verb",
                "lines":[],"elapsed_s":0.01}"""
        ).getOrThrow()
        assertFalse(e.ok)
        assertTrue(e.verdict.contains("not exposed"))
    }

    @Test
    fun `a null target survives`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"quota","target":null,"verdict":"x",
                "lines":[],"elapsed_s":0.0}"""
        ).getOrThrow()
        assertEquals(null, e.target)
    }

    @Test
    fun `garbage is a failure, not a crash`() {
        assertTrue(Envelope.parse("not json at all").isFailure)
        assertTrue(Envelope.parse("").isFailure)
    }

    @Test
    fun `raw keeps the extra keys verbs attach`() {
        // status carries `doc`, starr carries `arrs`, quota carries percent.
        // The typed fields cover the six every verb has; `raw` is how screens
        // reach the rest without Envelope growing a field per verb.
        val e = Envelope.parse(
            """{"ok":true,"verb":"quota","target":null,"verdict":"x","lines":[],
                "elapsed_s":0.0,"used_gb":1800.0,"total_gb":2794.0,"percent":64.4}"""
        ).getOrThrow()
        assertTrue(e.raw.containsKey("percent"))
    }
}
