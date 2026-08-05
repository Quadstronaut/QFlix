package com.qflix.heartbeat.net

/**
 * Test/preview double for [StatusTransport]. Returns a fixed per-verb
 * [Result] instead of touching the network, proving the ViewModel/UI layer
 * only ever needs to depend on the [StatusTransport] interface — never on
 * [SshFetcher] directly.
 *
 * Scripted with a verb -> body map so a single fake can stand in for a whole
 * session's worth of different verbs (e.g. a `status` fetch followed by an
 * `app.restart`), not just one canned response.
 */
class FakeTransport(private val results: Map<String, Result<String>>) : StatusTransport {

    /** Convenience for the common single-verb case ("status" unless given). */
    constructor(result: Result<String>, verb: String = "status") : this(mapOf(verb to result))

    override suspend fun exec(verb: String): Result<String> =
        results[verb] ?: Result.failure(IllegalArgumentException("FakeTransport has no scripted result for verb '$verb'"))
}
