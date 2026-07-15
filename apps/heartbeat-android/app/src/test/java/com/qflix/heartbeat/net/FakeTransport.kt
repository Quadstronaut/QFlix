package com.qflix.heartbeat.net

/**
 * Test/preview double for [StatusTransport]. Returns a fixed [Result]
 * instead of touching the network, proving the ViewModel/UI layer only ever
 * needs to depend on the [StatusTransport] interface — never on [SshFetcher]
 * directly.
 */
class FakeTransport(private val result: Result<String>) : StatusTransport {
    override suspend fun fetch(): Result<String> = result
}
