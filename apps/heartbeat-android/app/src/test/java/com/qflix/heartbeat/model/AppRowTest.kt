package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Test

class AppRowTest {
    @Test
    fun `parses the app_list lines into slug and class`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"app.list","target":null,
                "verdict":"24 apps with a lifecycle",
                "lines":["sonarr ucc","listmonk systemd"],"elapsed_s":0.1}""",
        ).getOrThrow()
        assertEquals(
            listOf(AppRow("sonarr", "ucc"), AppRow("listmonk", "systemd")),
            AppRow.parseList(e),
        )
    }

    @Test
    fun `a malformed line is skipped, not crashed on`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"app.list","target":null,"verdict":"x",
                "lines":["sonarr ucc","garbage"],"elapsed_s":0.1}""",
        ).getOrThrow()
        assertEquals(listOf(AppRow("sonarr", "ucc")), AppRow.parseList(e))
    }
}
