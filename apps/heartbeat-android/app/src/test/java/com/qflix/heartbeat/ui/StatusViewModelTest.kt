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
        val vm = StatusViewModel(FakeTransport(Result.success(readFixture())))

        val state = vm.uiState.value as StatusUiState.Ready
        val quota = state.dashboard.quota as SectionState.Ok
        assertEquals("2074 / 2794 GB", quota.data.diskLabel)
        assertFalse(vm.isRefreshing.value)
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
                Result.success(readFixture()),
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
                Result.success(readFixture()),
                Result.success(readFixture()),
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
                Result.success(readFixture()),
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
        // just refresh-after-Ready.
        val transport = GatedTransport(Result.success(readFixture()))
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
        assertEquals(1, transport.callCount)
    }
}
