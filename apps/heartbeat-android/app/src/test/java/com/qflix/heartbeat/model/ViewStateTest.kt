package com.qflix.heartbeat.model

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure StatusDoc -> DashboardState mapping tests: the live fixture end to
 * end, hand-built degraded docs (per-section Ok/SectionError/Missing), and
 * the formatting edge cases called out in the plan (fraction text, hours,
 * disk/bandwidth bars, alert ordering, data age).
 */
class ViewStateTest {

    private fun readFixture(): String =
        checkNotNull(javaClass.getResourceAsStream("/app_status_live.json")) {
            "app_status_live.json missing from test resources"
        }.bufferedReader().readText()

    // -- end to end against the live fixture --

    @Test
    fun `live fixture maps to Ok sections with the expected summaries`() {
        val doc = StatusDoc.parse(readFixture())
        val now = Instant.parse(doc.meta!!.generatedAt)

        val state = ViewState.from(doc, now)

        val quota = state.quota as SectionState.Ok
        assertEquals("2074 / 2794 GB", quota.data.diskLabel)
        assertEquals("3.5% used - resets Jul 28", quota.data.bandwidthLabel)

        val kuma = state.kuma as SectionState.Ok
        assertEquals("51/55 up", kuma.data.summary)
        assertEquals(4, kuma.data.red.size)

        val streams = state.streams as SectionState.Ok
        assertEquals("1/1", streams.data.fraction)

        val top5 = state.top5 as SectionState.Ok
        assertEquals("79.1 h", top5.data.watch[0].hoursLabel)

        val downloads = state.downloads as SectionState.Ok
        assertEquals(4, downloads.data.stuck.size)
        assertEquals("cbed175c", downloads.data.stuck[0].hash8)
        assertEquals("3h", downloads.data.stuck[0].hoursLabel)
        assertTrue(downloads.data.stuck[0].acted)
        assertEquals(5, downloads.data.recentUnsticks.size)

        assertEquals("0s ago", state.dataAge)
    }

    // -- maint section (v2): failed units + anime-janitor activity --

    @Test
    fun `maint maps failed units and janitor label`() {
        val json = """
            {"maint": {"ok": true, "failed_units": ["manitoba-maint-reaper.service"],
                       "anime_janitor": {"recent_moves": 2,
                           "last_move": {"title": "Cowboy Bebop (2021)", "from": "sonarr2", "to": "sonarr"}}}}
        """.trimIndent()
        val doc = StatusDoc.parse(json)
        val state = ViewState.from(doc, Instant.parse("2026-07-25T20:00:00Z"))
        val maint = state.maint as SectionState.Ok
        assertEquals(listOf("manitoba-maint-reaper.service"), maint.data.failedUnits)
        assertTrue(maint.data.janitorLabel!!.contains("Cowboy Bebop (2021)"))
        assertTrue(maint.data.janitorLabel!!.contains("2 re-home"))
    }

    @Test
    fun `maint is Missing when section absent`() {
        val doc = StatusDoc.parse("{}")
        val state = ViewState.from(doc, Instant.parse("2026-07-25T20:00:00Z"))
        assertTrue(state.maint is SectionState.Missing)
    }

    // -- per-section render state: Ok / SectionError / Missing --

    @Test
    fun `a section with ok false renders SectionError, not a crash`() {
        val json = """
            {"meta": {"version": 1, "host": "manitoba", "generated_at": "2026-07-15T23:48:07Z"},
             "streams": {"ok": false, "error": "tautulli unreachable"}}
        """.trimIndent()
        val doc = StatusDoc.parse(json)

        val state = ViewState.from(doc, Instant.parse("2026-07-15T23:48:07Z"))

        val streams = state.streams as SectionState.SectionError
        assertEquals("tautulli unreachable", streams.message)
    }

    @Test
    fun `a section with ok false and no error message still renders SectionError with a fallback`() {
        val json = """{"downloads": {"ok": false}}"""
        val doc = StatusDoc.parse(json)

        val state = ViewState.from(doc, Instant.now())

        assertTrue(state.downloads is SectionState.SectionError)
    }

    @Test
    fun `a missing section renders Missing`() {
        val doc = StatusDoc.parse("""{"meta": {"version": 1}}""")

        val state = ViewState.from(doc, Instant.now())

        assertEquals(SectionState.Missing, state.quota)
        assertEquals(SectionState.Missing, state.kuma)
        assertEquals(SectionState.Missing, state.streams)
        assertEquals(SectionState.Missing, state.top5)
        assertEquals(SectionState.Missing, state.downloads)
    }

    @Test
    fun `an empty doc never crashes ViewState mapping`() {
        val doc = StatusDoc.parse("{}")

        val state = ViewState.from(doc, Instant.now())

        assertEquals(SectionState.Missing, state.quota)
        assertTrue(state.alerts.isEmpty())
    }

    // -- streams fraction edge cases --

    @Test
    fun `zero streams and zero users renders 0-0, not an error`() {
        val doc = StatusDoc.parse(
            """{"streams": {"ok": true, "error": null, "streams": 0, "users": 0}}""",
        )

        val state = ViewState.from(doc, Instant.now())

        val streams = state.streams as SectionState.Ok
        assertEquals("0/0", streams.data.fraction)
    }

    @Test
    fun `four streams and three users renders 4-3`() {
        val doc = StatusDoc.parse(
            """{"streams": {"ok": true, "error": null, "streams": 4, "users": 3}}""",
        )

        val state = ViewState.from(doc, Instant.now())

        val streams = state.streams as SectionState.Ok
        assertEquals("4/3", streams.data.fraction)
    }

    // -- watch hours formatting --

    @Test
    fun `watch hours format to one decimal with an h suffix`() {
        assertEquals("78.2 h", ViewState.formatHours(78.2))
        assertEquals("0.0 h", ViewState.formatHours(0.0))
        assertEquals("100.0 h", ViewState.formatHours(100.0))
    }

    // -- disk bar --

    @Test
    fun `disk label formats as used slash total GB`() {
        assertEquals("2073 / 2794 GB", ViewState.diskLabel(2073.0, 2794.0))
    }

    // -- bandwidth bar --

    @Test
    fun `bandwidth label formats used pct and reset date from the contract sample`() {
        assertEquals(
            "3.4% used - resets Jul 28",
            ViewState.bandwidthLabel(3.42, "2026-07-28T00:00:00"),
        )
    }

    @Test
    fun `bandwidth label omits the reset clause when next_reset is missing`() {
        assertEquals("3.4% used", ViewState.bandwidthLabel(3.42, null))
    }

    // -- alert ordering --

    @Test
    fun `alerts are ordered crit before warn, preserving relative order within each level`() {
        val alerts = listOf(
            Alert(level = "warn", text = "w1"),
            Alert(level = "crit", text = "c1"),
            Alert(level = "warn", text = "w2"),
            Alert(level = "crit", text = "c2"),
        )

        val ordered = ViewState.orderAlerts(alerts)

        assertEquals(listOf("c1", "c2", "w1", "w2"), ordered.map { it.text })
    }

    @Test
    fun `empty alerts list stays empty, meaning all clear`() {
        assertTrue(ViewState.orderAlerts(emptyList()).isEmpty())
    }

    // -- data age --

    @Test
    fun `data age under a minute renders in seconds`() {
        val generated = "2026-07-15T23:48:00Z"
        val now = Instant.parse("2026-07-15T23:48:45Z")

        assertEquals("45s ago", ViewState.dataAge(generated, now))
    }

    @Test
    fun `data age of two minutes renders as 2m ago`() {
        val generated = "2026-07-15T23:46:00Z"
        val now = Instant.parse("2026-07-15T23:48:05Z")

        assertEquals("2m ago", ViewState.dataAge(generated, now))
    }

    @Test
    fun `data age of hours renders as Nh ago`() {
        val generated = "2026-07-15T20:00:00Z"
        val now = Instant.parse("2026-07-15T23:00:00Z")

        assertEquals("3h ago", ViewState.dataAge(generated, now))
    }

    @Test
    fun `data age with an unparseable timestamp does not crash`() {
        assertEquals("unknown", ViewState.dataAge("not-a-timestamp", Instant.now()))
    }

    @Test
    fun `data age with a null timestamp does not crash`() {
        assertEquals("unknown", ViewState.dataAge(null, Instant.now()))
    }
}
