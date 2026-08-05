package com.qflix.heartbeat.ui

import com.qflix.heartbeat.net.FakeTransport
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [ActionViewModel] state-transition tests, mirroring [StatusViewModelTest]'s
 * setup: [ActionViewModel.fire] launches on viewModelScope, which resolves
 * Dispatchers.Main - an [UnconfinedTestDispatcher] installed as Main runs
 * that launch eagerly to completion before [fire] returns (FakeTransport's
 * exec() never really suspends), so [ActionViewModel.state] reads the landed
 * value with no advanceUntilIdle() needed.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ActionViewModelTest {

    @Before
    fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `a refused verb is Done with ok=false, not Failed`() = runTest {
        // The server refusing is an ANSWER. Failed is reserved for the
        // transport never getting one.
        val t = FakeTransport(
            mapOf(
                "logs listmonk" to Result.success(
                    """{"ok":false,"verb":"logs","target":"listmonk",
                        "verdict":"listmonk logs are not exposed over this verb",
                        "lines":[],"elapsed_s":0.0}""",
                ),
            ),
        )
        val vm = ActionViewModel(t)
        vm.fire("logs listmonk")
        val s = vm.state.value
        assertTrue(s is ActionState.Done)
        assertEquals(false, (s as ActionState.Done).envelope.ok)
    }

    @Test
    fun `a transport failure is Failed`() = runTest {
        val t = FakeTransport(emptyMap()) // returns Result.failure
        val vm = ActionViewModel(t)
        vm.fire("status")
        assertTrue(vm.state.value is ActionState.Failed)
    }

    @Test
    fun `unparseable body is Failed, not a crash`() = runTest {
        val t = FakeTransport(mapOf("status" to Result.success("<html>gateway timeout</html>")))
        val vm = ActionViewModel(t)
        vm.fire("status")
        assertTrue(vm.state.value is ActionState.Failed)
    }
}
