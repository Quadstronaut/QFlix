package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parses [StatusDoc] against the real live fixture captured off the box
 * (tests/fixtures/app_status_live.json, mirrored into src/test/resources)
 * plus a handful of hand-built degraded documents, proving the model never
 * throws on a partial/failed section.
 */
class StatusDocTest {

    private fun readFixture(): String =
        checkNotNull(javaClass.getResourceAsStream("/app_status_live.json")) {
            "app_status_live.json missing from test resources"
        }.bufferedReader().readText()

    // -- live fixture: real values, not fabricated --

    @Test
    fun `parses the live fixture without throwing`() {
        val doc = StatusDoc.parse(readFixture())
        assertNotNull(doc)
    }

    @Test
    fun `live fixture meta matches the contract`() {
        val doc = StatusDoc.parse(readFixture())

        assertEquals(1, doc.meta?.version)
        assertEquals("manitoba", doc.meta?.host)
        assertEquals("2026-07-15T23:48:07Z", doc.meta?.generatedAt)
        assertEquals(2589L, doc.meta?.elapsedMs)
    }

    @Test
    fun `live fixture quota numbers match the contract`() {
        val doc = StatusDoc.parse(readFixture())

        val quota = requireNotNull(doc.quota)
        assertTrue(quota.ok)
        assertEquals(2074.0, quota.disk?.usedGb!!, 0.001)
        assertEquals(2794.0, quota.disk?.totalGb!!, 0.001)
        assertEquals(74.2, quota.disk?.pct!!, 0.001)
        assertEquals(3.49, quota.bandwidth?.usedPct!!, 0.001)
        assertEquals(96.51, quota.bandwidth?.availablePct!!, 0.001)
        assertEquals("2026-07-28T00:00:00", quota.bandwidth?.nextReset)
    }

    @Test
    fun `live fixture kuma totals match the contract`() {
        val doc = StatusDoc.parse(readFixture())

        val kuma = requireNotNull(doc.kuma)
        assertTrue(kuma.ok)
        assertEquals(55, kuma.total)
        assertEquals(51, kuma.up)
        assertEquals(4, kuma.down)
        assertEquals(4, kuma.red.size)
        assertEquals("Quadstronix", kuma.red[0].name)
        assertEquals("QFlix Reaper", kuma.red[3].name)
    }

    @Test
    fun `live fixture streams section parses`() {
        val doc = StatusDoc.parse(readFixture())

        val streams = requireNotNull(doc.streams)
        assertTrue(streams.ok)
        assertEquals(1, streams.streams)
        assertEquals(1, streams.users)
        assertEquals(1, streams.transcodes)
        assertEquals(14994, streams.wanKbps)
    }

    @Test
    fun `live fixture top5 lists parse in order`() {
        val doc = StatusDoc.parse(readFixture())

        val top5 = requireNotNull(doc.top5)
        assertEquals(5, top5.requests30d.size)
        assertEquals("Brinton Family", top5.requests30d[0].user)
        assertEquals(14, top5.requests30d[0].count)
        assertEquals(5, top5.watch30d.size)
        assertEquals("BAsylum", top5.watch30d[0].user)
        assertEquals(79.1, top5.watch30d[0].hours!!, 0.001)
        assertEquals(106, top5.watch30d[0].plays)
    }

    @Test
    fun `live fixture downloads section parses qbit sab stuck and unsticks`() {
        val doc = StatusDoc.parse(readFixture())

        val downloads = requireNotNull(doc.downloads)
        assertTrue(downloads.ok)
        assertEquals(12, downloads.qbit?.total)
        assertEquals(1, downloads.qbit?.active)
        assertEquals(10, downloads.qbit?.seeding)
        assertEquals(1, downloads.sab?.queued)
        assertFalse(downloads.sab?.paused!!)
        assertEquals(4, downloads.stuck.size)
        assertEquals("cbed175c", downloads.stuck[0].hash8)
        assertEquals(3.0, downloads.stuck[0].hours!!, 0.001)
        assertTrue(downloads.stuck[0].acted!!)
        assertEquals(5, downloads.recentUnsticks.size)
        assertEquals("f0a3658d", downloads.recentUnsticks[0].hash8)
        assertEquals("qbit-orphan-removed", downloads.recentUnsticks[0].result)
    }

    @Test
    fun `live fixture alerts parse with level and text`() {
        val doc = StatusDoc.parse(readFixture())

        assertEquals(3, doc.alerts.size)
        assertEquals("crit", doc.alerts[0].level)
        assertEquals("warn", doc.alerts[2].level)
    }

    // -- degraded docs: partial failure must never throw --

    @Test
    fun `section with ok false and populated error parses without throwing`() {
        val json = """
            {"meta": {"version": 1, "host": "manitoba"},
             "streams": {"ok": false, "error": "tautulli unreachable"}}
        """.trimIndent()

        val doc = StatusDoc.parse(json)

        assertNotNull(doc.streams)
        assertFalse(doc.streams!!.ok)
        assertEquals("tautulli unreachable", doc.streams?.error)
    }

    @Test
    fun `a missing top-level section decodes to null rather than throwing`() {
        val json = """{"meta": {"version": 1, "host": "manitoba"}}"""

        val doc = StatusDoc.parse(json)

        assertNull(doc.quota)
        assertNull(doc.kuma)
        assertNull(doc.streams)
        assertNull(doc.top5)
        assertNull(doc.downloads)
        assertTrue(doc.alerts.isEmpty())
    }

    @Test
    fun `unknown extra keys anywhere in the doc are ignored, not fatal`() {
        val json = """
            {"meta": {"version": 1, "host": "manitoba", "unexpected_field": "x"},
             "quota": {"ok": true, "error": null,
                       "disk": {"used_gb": 100, "total_gb": 200, "pct": 50.0, "extra": true},
                       "bandwidth": {"used_pct": 1.0, "available_pct": 99.0}},
             "totally_unknown_top_level_section": {"whatever": 1}}
        """.trimIndent()

        val doc = StatusDoc.parse(json)

        assertEquals(1, doc.meta?.version)
        assertEquals(100.0, doc.quota?.disk?.usedGb!!, 0.001)
    }

    @Test
    fun `an empty doc parses to an all-null all-empty StatusDoc`() {
        val doc = StatusDoc.parse("{}")

        assertNull(doc.meta)
        assertNull(doc.quota)
        assertTrue(doc.alerts.isEmpty())
    }
}
