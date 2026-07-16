package com.qflix.heartbeat.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.qflix.heartbeat.model.DashboardState
import com.qflix.heartbeat.model.StatusDoc
import com.qflix.heartbeat.model.ViewState
import com.qflix.heartbeat.net.StatusTransport
import java.time.Instant
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Everything the dashboard screen (A4) can be showing at any moment.
 *  - [Loading]: no data yet - first launch, or a retry after [Error].
 *  - [Ready]: last fetch succeeded; [fetchedAt] backs the "data age" clock
 *    that A2's [ViewState.dataAge] renders relative to "now".
 *  - [Error]: last fetch failed (unreachable host, not provisioned, bad
 *    auth, malformed JSON - [StatusTransport.fetch] never throws, so this
 *    covers every failure mode). [retry] re-runs the fetch without the UI
 *    needing a reference to the ViewModel itself.
 */
sealed class StatusUiState {
    object Loading : StatusUiState()
    data class Ready(val dashboard: DashboardState, val fetchedAt: Instant) : StatusUiState()
    data class Error(val message: String, val retry: () -> Unit) : StatusUiState()
}

/**
 * Owns the fetch -> parse -> format pipeline end to end: [StatusTransport]
 * hands back raw JSON, [StatusDoc.parse] turns it into the wire model (A2),
 * [ViewState.from] turns that into the pre-formatted [DashboardState] the UI
 * renders. The UI layer never touches StatusDoc, StatusTransport, or the
 * network directly - only [uiState] and [isRefreshing].
 */
class StatusViewModel(private val transport: StatusTransport) : ViewModel() {

    private val _uiState = MutableStateFlow<StatusUiState>(StatusUiState.Loading)
    val uiState: StateFlow<StatusUiState> = _uiState.asStateFlow()

    // Separate from uiState so pull-to-refresh can show a spinner over the
    // existing dashboard instead of blanking it back to a full-screen
    // Loading state - only a first load or a retry-after-error does that.
    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    init {
        load()
    }

    /** Pull-to-refresh entry point, and what [StatusUiState.Error.retry] calls too. */
    fun refresh() {
        if (_uiState.value is StatusUiState.Ready) {
            viewModelScope.launch {
                _isRefreshing.value = true
                fetchAndPublish()
                _isRefreshing.value = false
            }
        } else {
            load()
        }
    }

    private fun load() {
        viewModelScope.launch {
            _uiState.value = StatusUiState.Loading
            fetchAndPublish()
        }
    }

    private suspend fun fetchAndPublish() {
        transport.fetch()
            .mapCatching { raw -> ViewState.from(StatusDoc.parse(raw), Instant.now()) }
            .fold(
                onSuccess = { dashboard -> _uiState.value = StatusUiState.Ready(dashboard, Instant.now()) },
                onFailure = { e -> _uiState.value = StatusUiState.Error(e.message ?: "Unknown error", ::refresh) },
            )
    }

    companion object {
        /** Simple factory - the only DI this app needs is "which StatusTransport". */
        fun factory(transport: StatusTransport): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    StatusViewModel(transport) as T
            }
    }
}
