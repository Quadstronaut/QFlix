package com.qflix.heartbeat.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DestinationTest {
    @Test
    fun `dashboard is first so existing muscle memory survives`() {
        assertEquals(Destination.DASHBOARD, Destination.values().first())
    }

    @Test
    fun `four destinations, labelled for the drawer`() {
        assertEquals(listOf("Dashboard", "Apps", "stARR", "Fix"),
            Destination.values().map { it.label })
    }
}
