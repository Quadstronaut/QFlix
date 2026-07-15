package com.qflix.heartbeat.net

/**
 * Decoupling seam between the network layer and everything that consumes it
 * (ViewModel, UI previews, tests).
 *
 * There is exactly one production implementation ([SshFetcher]), which opens
 * an SSH connection to the seedbox and returns the raw status JSON document.
 * Tests and Compose previews use a fake instead, so nothing above this
 * interface ever needs a real network connection or provisioned device to
 * exercise its logic.
 */
interface StatusTransport {
    /**
     * Fetches the raw status JSON document as a string.
     *
     * Never throws — every failure (missing provisioning, unreachable host,
     * auth rejection, timeout, malformed response) is reported through
     * [Result.failure] so callers can render a per-section error state
     * instead of crashing.
     */
    suspend fun fetch(): Result<String>
}
