package com.qflix.heartbeat.net

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Proves the [StatusTransport] seam itself: anything coded against the
 * interface can swap [SshFetcher] for [FakeTransport] and get a canned
 * result with zero network/provisioning setup. Verb is irrelevant to the
 * seam itself, so both tests exercise "status".
 */
class StatusTransportSeamTest {

    private val canned = """{"meta":{"version":1,"host":"manitoba"}}"""

    @Test
    fun `fake transport returns the canned JSON string unchanged`() = runBlocking {
        val transport: StatusTransport = FakeTransport(Result.success(canned))

        val result = transport.exec("status")

        assertTrue(result.isSuccess)
        assertEquals(canned, result.getOrThrow())
    }

    @Test
    fun `fake transport can also simulate failure without touching real transport`() = runBlocking {
        val transport: StatusTransport = FakeTransport(Result.failure(RuntimeException("offline")))

        val result = transport.exec("status")

        assertTrue(result.isFailure)
    }
}
