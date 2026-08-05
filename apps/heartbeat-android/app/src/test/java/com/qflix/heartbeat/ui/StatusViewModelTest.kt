package com.qflix.heartbeat.ui

import com.qflix.heartbeat.model.SectionState
import com.qflix.heartbeat.net.FakeTransport
import com.qflix.heartbeat.net.StatusTransport
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [StatusViewModel] state-transition tests. No Compose, no real network -
 * [FakeTransport] stands in for [StatusTransport] end to end (A2's
 * StatusDoc.parse + ViewState.from run for real, only the transport is
 * faked). viewModelScope resolves Dispatchers.Main to an
 * [UnconfinedTestDispatcher] so every launched coroutine runs eagerly to
 * completion on the test thread - no advanceUntilIdle() needed.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class StatusViewModelTest {

    private fun readFixture(): String =
        checkNotNull(javaClass.getResourceAsStream("/app_status_live.json")) {
            "app_status_live.json missing from test resources"
        }.bufferedReader().readText()

    /**
     * `status` now returns an [Envelope][com.qflix.heartbeat.model.Envelope],
     * not a bare doc - wraps the live fixture's doc JSON the same way
     * dispatch.py's `_verb_status` does (`env["doc"] = ...`), so every test
     * below that scripts a successful status fetch exercises the real
     * unwrap path rather than the pre-Task-7 shape.
     */
    private fun statusEnvelope(doc: String = readFixture()): String =
        """{"ok":true,"verb":"status","target":null,"verdict":"status doc emitted",
            "lines":[],"elapsed_s":0.0,"doc":$doc}"""

    /** A successful `quota` verb envelope - used_gb/total_gb/percent live at the top level, not under `doc`. */
    private fun quotaEnvelope(usedGb: Double = 2074.0, totalGb: Double = 2794.0, percent: Double = 74.2): String =
        """{"ok":true,"verb":"quota","target":null,"verdict":"x","lines":[],"elapsed_s":0.0,
            "used_gb":$usedGb,"total_gb":$totalGb,"percent":$percent}"""

    /** Returns queued results in order, one per [exec] call - lets a test simulate retry-after-failure sequences. */
    private class QueueTransport(private val results: MutableList<Result<String>>) : StatusTransport {
        override suspend fun exec(verb: String): Result<String> = results.removeAt(0)
    }

    /**
     * A [StatusTransport] whose [exec] suspends until [release] is called,
     * counting how many times it was actually invoked. Used to prove
     * [StatusViewModel]'s re-entrancy guard collapses rapid-fire refresh()
     * calls into at most one in-flight transport call, instead of racing two
     * concurrent SSH sessions.
     */
    private class GatedTransport(private val result: Result<String>) : StatusTransport {
        var callCount: Int = 0
            private set
        private val gate = CompletableDeferred<Unit>()

        fun release() {
            gate.complete(Unit)
        }

        override suspend fun exec(verb: String): Result<String> {
            callCount++
            gate.await()
            return result
        }
    }

    @Before
    fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `fetches on init and lands on Ready with the mapped dashboard`() = runTest {
        val vm = StatusViewModel(FakeTransport(Result.success(statusEnvelope())))

        val state = vm.uiState.value as StatusUiState.Ready
        // No "quota" verb scripted on this single-verb FakeTransport, so the
        // quota tile falls back to the doc's own quota.disk reading.
        val quota = state.dashboard.quota as SectionState.Ok
        assertEquals("2074 / 2794 GB", quota.data.diskLabel)
        assertFalse(vm.isRefreshing.value)
    }

    @Test
    fun `the quota verb's reading overrides the doc's own disk figure`() = runTest {
        val vm = StatusViewModel(
            FakeTransport(
                mapOf(
                    "status" to Result.success(statusEnvelope()),
                    "quota" to Result.success(quotaEnvelope(usedGb = 1800.0, totalGb = 2794.0, percent = 64.4)),
                ),
            ),
        )

        val state = vm.uiState.value as StatusUiState.Ready
        val quota = state.dashboard.quota as SectionState.Ok
        assertEquals("1800 / 2794 GB", quota.data.diskLabel)
    }

    @Test
    fun `a failed initial fetch lands on Error carrying a retry callback`() = runTest {
        val vm = StatusViewModel(FakeTransport(Result.failure(IllegalStateException("device not provisioned"))))

        val state = vm.uiState.value as StatusUiState.Error
        assertEquals("device not provisioned", state.message)
    }

    @Test
    fun `Loading is the state before any fetch completes`() = runTest {
        // A transport that suspends forever proves the ViewModel starts in
        // (and stays in) Loading until the fetch actually resolves.
        val hangingTransport = object : StatusTransport {
            override suspend fun exec(verb: String): Result<String> = suspendCancellableCoroutine { }
        }

        val vm = StatusViewModel(hangingTransport)

        assertTrue(vm.uiState.value is StatusUiState.Loading)
    }

    @Test
    fun `calling retry from an Error state re-fetches and can succeed`() = runTest {
        val transport = QueueTransport(
            mutableListOf(
                Result.failure(IllegalStateException("offline")),
                Result.success(statusEnvelope()),
                Result.success(quotaEnvelope()),
            ),
        )
        val vm = StatusViewModel(transport)
        val errorState = vm.uiState.value as StatusUiState.Error

        errorState.retry()

        assertTrue(vm.uiState.value is StatusUiState.Ready)
    }

    @Test
    fun `refresh after a Ready state re-fetches and stays Ready with fresh data, toggling isRefreshing`() = runTest {
        val transport = QueueTransport(
            mutableListOf(
                Result.success(statusEnvelope()),
                Result.success(quotaEnvelope()),
                Result.success(statusEnvelope()),
                Result.success(quotaEnvelope()),
            ),
        )
        val vm = StatusViewModel(transport)
        check(vm.uiState.value is StatusUiState.Ready) { "precondition: first fetch must succeed" }

        vm.refresh()

        assertTrue(vm.uiState.value is StatusUiState.Ready)
        assertFalse(vm.isRefreshing.value)
    }

    @Test
    fun `refresh from a Loading or Error state behaves like a normal reload`() = runTest {
        val transport = QueueTransport(
            mutableListOf(
                Result.failure(IllegalStateException("offline")),
                Result.success(statusEnvelope()),
                Result.success(quotaEnvelope()),
            ),
        )
        val vm = StatusViewModel(transport)
        check(vm.uiState.value is StatusUiState.Error)

        vm.refresh()

        assertTrue(vm.uiState.value is StatusUiState.Ready)
    }

    @Test
    fun `two rapid refresh calls collapse into a single in-flight transport fetch`() = runTest {
        // The transport gates on the very first fetch (init's own load()) so
        // this also proves the guard covers "initial load included", not
        // just refresh-after-Ready. GatedTransport answers every verb with
        // the same scripted result, so once the gate opens the pending
        // `status` call resolves, then the ensuing `quota` call resolves
        // immediately too (the gate stays completed) - one fetch is 2 calls
        // now, not 1, which is what the final assertion below checks for.
        val transport = GatedTransport(Result.success(statusEnvelope()))
        val vm = StatusViewModel(transport)
        assertEquals(1, transport.callCount)
        assertTrue("still loading until the gate is released", vm.uiState.value is StatusUiState.Loading)

        // Two rapid refresh() calls while a fetch is already in flight - both
        // must be no-ops; neither should launch a second concurrent fetch.
        vm.refresh()
        vm.refresh()
        assertEquals(1, transport.callCount)

        transport.release()

        assertTrue(vm.uiState.value is StatusUiState.Ready)
        assertEquals(2, transport.callCount)
    }
}
