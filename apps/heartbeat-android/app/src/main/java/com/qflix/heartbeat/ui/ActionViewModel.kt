package com.qflix.heartbeat.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.qflix.heartbeat.model.Envelope
import com.qflix.heartbeat.net.StatusTransport
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Every state one fired verb can be in.
 *  - [Idle]: nothing fired yet - no [VerdictSheet] to show.
 *  - [Running]: in flight; [verb] is what the sheet's header names while
 *    waiting.
 *  - [Done]: the transport got an answer, `ok:true` OR `ok:false` alike. A
 *    refused verb ("unstick refused: sonarr is red") is data to render, not
 *    a failure - see [Envelope]'s own kdoc for why.
 *  - [Failed]: the transport never got an answer at all - unreachable host,
 *    a dropped connection, a body that didn't parse as an envelope. This is
 *    the ONLY state standing in for something going wrong; every answer the
 *    server actually gave, refusal included, is [Done].
 */
sealed class ActionState {
    object Idle : ActionState()
    data class Running(val verb: String) : ActionState()
    data class Done(val envelope: Envelope) : ActionState()
    data class Failed(val message: String) : ActionState()
}

/**
 * Fires one dispatcher verb and tracks its outcome for [VerdictSheet] to
 * render. Shared across the Apps (Task 5) and stARR (Task 6) screens: every
 * row action goes through this one `fire(verb)` -> one verdict sheet,
 * rather than each screen owning its own request/response plumbing.
 */
class ActionViewModel(private val transport: StatusTransport) : ViewModel() {

    private val _state = MutableStateFlow<ActionState>(ActionState.Idle)
    val state: StateFlow<ActionState> = _state.asStateFlow()

    /**
     * Runs [verb] end to end: [ActionState.Running] -> ([ActionState.Done] |
     * [ActionState.Failed]). [Envelope.parse] failing (garbage body) is
     * folded into the same [Result.failure] path as a transport error, so
     * both land on [ActionState.Failed] - only a body that parses at all,
     * `ok` true or false, reaches [ActionState.Done].
     */
    fun fire(verb: String) {
        _state.value = ActionState.Running(verb)
        viewModelScope.launch {
            transport.exec(verb)
                .mapCatching { body -> Envelope.parse(body).getOrThrow() }
                .fold(
                    onSuccess = { envelope -> _state.value = ActionState.Done(envelope) },
                    onFailure = { e -> _state.value = ActionState.Failed(e.message ?: "Unknown error") },
                )
        }
    }

    /** Back to [ActionState.Idle] - what dismissing [VerdictSheet] calls. */
    fun reset() {
        _state.value = ActionState.Idle
    }

    companion object {
        /** Simple factory - same shape as [StatusViewModel.factory]. */
        fun factory(transport: StatusTransport): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    ActionViewModel(transport) as T
            }
    }
}
