package com.qflix.heartbeat.net

/**
 * Decoupling seam between the network layer and everything that consumes it
 * (ViewModel, UI previews, tests).
 *
 * There is exactly one production implementation ([SshFetcher]), which opens
 * an SSH connection to the seedbox and returns the raw JSON envelope for
 * whichever verb was requested. Tests and Compose previews use a fake
 * instead, so nothing above this interface ever needs a real network
 * connection or provisioned device to exercise its logic.
 */
interface StatusTransport {
    /**
     * Runs one dispatcher verb and returns its raw JSON envelope.
     *
     * Never throws - every failure (missing provisioning, unreachable host,
     * auth rejection, timeout) is [Result.failure]. A verb that ran and
     * answered `ok:false` is [Result.success] carrying that envelope: the
     * server refusing is not a transport failure.
     */
    suspend fun exec(verb: String): Result<String>
}
