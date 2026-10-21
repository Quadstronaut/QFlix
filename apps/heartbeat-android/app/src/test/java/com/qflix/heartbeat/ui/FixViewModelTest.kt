package com.qflix.heartbeat.ui

import com.qflix.heartbeat.net.FireResult
import com.qflix.heartbeat.net.RoutineFirer
import java.io.File
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

@OptIn(ExperimentalCoroutinesApi::class)
class FixViewModelTest {

    @get:Rule
    val tmp = TemporaryFolder()

    @Before
    fun setUp() = Dispatchers.setMain(UnconfinedTestDispatcher())

    @After
    fun tearDown() = Dispatchers.resetMain()

    private class GatedFirer(var result: Result<FireResult>) : RoutineFirer {
        var calls = 0
        val gate = CompletableDeferred<Unit>().apply { complete(Unit) }
        override suspend fun fire(text: String): Result<FireResult> {
            calls++
            gate.await()
            return result
        }
    }

    private fun history() = FixHistory(File(tmp.newFolder(), "h.json"))

    @Test
    fun `success records history newest first and exposes the session url`() {
        val firer = GatedFirer(Result.success(FireResult("s1", "https://claude.ai/code/s1")))
        val vm = FixViewModel(firer, history(), clock = { 1000L })
        vm.send("  fix sonarr ")
        assertEquals(FixState.Fired("https://claude.ai/code/s1"), vm.state.value)
        assertEquals(listOf(FixEntry(1000L, "fix sonarr", "https://claude.ai/code/s1")), vm.entries.value)
    }

    @Test
    fun `failure surfaces the message and records nothing`() {
        val firer = GatedFirer(Result.failure(IllegalStateException("HTTP 429: rate limited")))
        val vm = FixViewModel(firer, history())
        vm.send("x")
        assertEquals(FixState.Failed("HTTP 429: rate limited"), vm.state.value)
        assertTrue(vm.entries.value.isEmpty())
    }

    @Test
    fun `double tap while sending fires once`() {
        val firer = object : RoutineFirer {
            var calls = 0
            val gate = CompletableDeferred<Unit>()
            override suspend fun fire(text: String): Result<FireResult> {
                calls++; gate.await(); return Result.success(FireResult("s", "u"))
            }
        }
        val vm = FixViewModel(firer, history())
        vm.send("a"); vm.send("a")
        assertEquals(FixState.Sending, vm.state.value)
        firer.gate.complete(Unit)
        assertEquals(1, firer.calls)
    }

    @Test
    fun `history caps at MAX and survives a corrupt file`() {
        val f = File(tmp.newFolder(), "h.json").apply { writeText("not json") }
        val h = FixHistory(f)
        assertTrue(h.load().isEmpty())
        repeat(FixHistory.MAX + 5) { h.add(FixEntry(it.toLong(), "p$it", "u$it")) }
        val loaded = h.load()
        assertEquals(FixHistory.MAX, loaded.size)
        assertEquals((FixHistory.MAX + 4).toLong(), loaded.first().firedAtEpochMs)
    }
}
