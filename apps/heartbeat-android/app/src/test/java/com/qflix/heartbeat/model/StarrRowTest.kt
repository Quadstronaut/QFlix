package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StarrRowTest {

    private val body = """
      {"ok":true,"verb":"starr","target":null,"verdict":"4 *arrs",
       "lines":[],"elapsed_s":0.7,
       "arrs":{
         "sonarr":{"peek":{"slug":"sonarr","kind":"series","ok":true,"error":"",
                           "titles":[{"title":"A","have":12,"total":30,"complete":false},
                                     {"title":"B","have":10,"total":10,"complete":true}]},
                   "usage":{"slug":"sonarr","bytes":1073741824,"human":"1.0 GB",
                            "title_count":2,"ok":true,"error":""}},
         "radarr2":{"peek":{"slug":"radarr2","kind":"movie","ok":false,
                            "error":"refused","titles":[]},
                    "usage":{"slug":"radarr2","bytes":0,"human":"0.0 B",
                             "title_count":0,"ok":false,"error":"refused"}}}}
    """.trimIndent()

    @Test
    fun `counts titles and completes per arr`() {
        val rows = StarrRow.parseAll(Envelope.parse(body).getOrThrow())
        val s = rows.first { it.slug == "sonarr" }
        assertEquals(2, s.titleCount)
        assertEquals(1, s.completeCount)
        assertEquals("1.0 GB", s.human)
        assertTrue(s.ok)
    }

    @Test
    fun `a degraded arr is a row, not a missing row`() {
        // One dead *arr must not blank the page - the server keeps ok=true
        // overall and marks the instance. The UI must show it as degraded.
        val rows = StarrRow.parseAll(Envelope.parse(body).getOrThrow())
        val r = rows.first { it.slug == "radarr2" }
        assertFalse(r.ok)
        assertEquals("refused", r.error)
    }

    @Test
    fun `no consumption data is read out of the payload`() {
        // Structural: StarrRow has no field that could hold a member identity.
        val fields = StarrRow::class.java.declaredFields.map { it.name.lowercase() }
        val banned = listOf("watch", "view", "user", "session", "played", "seen")
        assertTrue(fields.none { f -> banned.any { f.contains(it) } })
    }
}
