package com.qflix.heartbeat.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.qflix.heartbeat.net.RoutineFirer
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json

/** One fired session, kept so the operator can reopen it to check in later. */
@Serializable
data class FixEntry(
    val firedAtEpochMs: Long,
    val prompt: String,
    val sessionUrl: String,
)

/**
 * Last [MAX] fired sessions, newest first, in `filesDir/fixer-history.json`.
 * A corrupt or missing file reads as empty - history is a convenience, never
 * a reason for the Fix page to refuse to fire.
 */
class FixHistory(private val file: File) {
    private val json = Json { ignoreUnknownKeys = true }
    private val serializer = ListSerializer(FixEntry.serializer())

    fun load(): List<FixEntry> =
        runCatching { json.decodeFromString(serializer, file.readText()) }.getOrDefault(emptyList())

    fun add(entry: FixEntry): List<FixEntry> {
        val next = (listOf(entry) + load()).take(MAX)
        file.parentFile?.mkdirs()
        file.writeText(json.encodeToString(serializer, next))
        return next
    }

    companion object {
        const val MAX = 20
    }
}

/**
 *  - [Idle]: nothing in flight.
 *  - [Sending]: `/fire` POST in flight; button disabled so one tap = one session.
 *  - [Fired]: session started; [sessionUrl] is what "Open session" launches.
 *  - [Failed]: no session started (not provisioned, HTTP error, rate cap).
 */
sealed class FixState {
    object Idle : FixState()
    object Sending : FixState()
    data class Fired(val sessionUrl: String) : FixState()
    data class Failed(val message: String) : FixState()
}

class FixViewModel(
    private val firer: RoutineFirer,
    private val history: FixHistory,
    private val clock: () -> Long = System::currentTimeMillis,
) : ViewModel() {

    private val _state = MutableStateFlow<FixState>(FixState.Idle)
    val state: StateFlow<FixState> = _state.asStateFlow()

    private val _entries = MutableStateFlow(history.load())
    val entries: StateFlow<List<FixEntry>> = _entries.asStateFlow()

    fun send(prompt: String) {
        // Re-entrancy guard: a double tap must not burn two of the 30/h fires.
        if (_state.value is FixState.Sending) return
        _state.value = FixState.Sending
        viewModelScope.launch {
            firer.fire(prompt).fold(
                onSuccess = { result ->
                    _entries.value = history.add(FixEntry(clock(), prompt.trim(), result.sessionUrl))
                    _state.value = FixState.Fired(result.sessionUrl)
                },
                onFailure = { e -> _state.value = FixState.Failed(e.message ?: "Unknown error") },
            )
        }
    }

    fun reset() {
        _state.value = FixState.Idle
    }

    companion object {
        fun factory(firer: RoutineFirer, history: FixHistory): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    FixViewModel(firer, history) as T
            }
    }
}
