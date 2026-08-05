package com.qflix.heartbeat.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.qflix.heartbeat.model.DashboardState
import com.qflix.heartbeat.model.Envelope
import com.qflix.heartbeat.model.QuotaTileReading
import com.qflix.heartbeat.model.StatusDoc
import com.qflix.heartbeat.model.ViewState
import com.qflix.heartbeat.net.StatusTransport
import java.time.Instant
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * Everything the dashboard screen (A4) can be showing at any moment.
 *  - [Loading]: no data yet - first launch, or a retry after [Error].
 *  - [Ready]: last fetch succeeded; [fetchedAt] backs the "data age" clock
 *    that A2's [ViewState.dataAge] renders relative to "now".
 *  - [Error]: last fetch failed (unreachable host, not provisioned, bad
 *    auth, malformed JSON - [StatusTransport.exec] never throws, so this
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

    // Tracks the single in-flight fetch (initial load, retry, or refresh) so
    // a double-tap on the refresh icon / a fast double pull-to-refresh can't
    // launch two concurrent transport.exec() calls. Without this, whichever
    // SSH session finishes second wins the final _uiState write - which can
    // silently regress the dashboard to older data if it happens to be the
    // earlier-triggered (slower) one. Only one fetch may be in flight at a
    // time, full stop - "in flight" includes the very first load() too.
    private var fetchJob: Job? = null

    init {
        load()
    }

    /** Pull-to-refresh entry point, and what [StatusUiState.Error.retry] calls too. */
    fun refresh() {
        if (fetchJob?.isActive == true) {
            return
        }
        if (_uiState.value is StatusUiState.Ready) {
            fetchJob = viewModelScope.launch {
                _isRefreshing.value = true
                try {
                    fetchAndPublish()
                } finally {
                    _isRefreshing.value = false
                }
            }
        } else {
            load()
        }
    }

    private fun load() {
        if (fetchJob?.isActive == true) {
            return
        }
        fetchJob = viewModelScope.launch {
            _uiState.value = StatusUiState.Loading
            fetchAndPublish()
        }
    }

    /**
     * `status` now returns an [Envelope], not a bare doc - the dashboard
     * document lives at `envelope.raw["doc"]` (present only when
     * `envelope.ok`; see dispatch.py's `_verb_status`). Everything below
     * that unwrap - [StatusDoc.parse], [ViewState.from] - is unchanged from
     * before this task.
     *
     * `quota` is fired ONLY after `status` lands successfully - there is no
     * dashboard to enhance with a quota tile if the main fetch already
     * failed, and firing it unconditionally would mean every transport
     * failure test needs a second scripted response it doesn't care about.
     * A quota-verb failure (refusal, transport error, missing fields) is
     * swallowed by [fetchQuotaOverride] into `null`: the tile degrades to
     * the status doc's own quota section rather than blanking the whole
     * dashboard over a tile that is a nice-to-have, not the main fetch.
     */
    private suspend fun fetchAndPublish() {
        val envelope = transport.exec("status")
            .mapCatching { raw -> Envelope.parse(raw).getOrThrow() }
            .getOrElse { e ->
                _uiState.value = StatusUiState.Error(e.message ?: "Unknown error", ::refresh)
                return
            }
        if (!envelope.ok) {
            _uiState.value = StatusUiState.Error(envelope.verdict, ::refresh)
            return
        }
        val docElement = envelope.raw["doc"]
        if (docElement == null) {
            _uiState.value = StatusUiState.Error("status envelope carried no doc", ::refresh)
            return
        }
        val doc = runCatching { StatusDoc.parse(docElement.toString()) }
            .getOrElse { e ->
                _uiState.value = StatusUiState.Error(e.message ?: "malformed status doc", ::refresh)
                return
            }

        val quotaOverride = fetchQuotaOverride()
        _uiState.value = StatusUiState.Ready(ViewState.from(doc, Instant.now(), quotaOverride), Instant.now())
    }

    /**
     * Best-effort: any failure (transport error, refusal, an envelope
     * missing the numeric fields) is `null`, never a thrown exception -
     * [ViewState.mapQuota] already knows how to fall back to the status
     * doc's own quota section when there is no override.
     */
    private suspend fun fetchQuotaOverride(): QuotaTileReading? {
        val envelope = transport.exec("quota")
            .mapCatching { raw -> Envelope.parse(raw).getOrThrow() }
            .getOrNull()
            ?.takeIf { it.ok }
            ?: return null
        val used = envelope.raw["used_gb"]?.jsonPrimitive?.doubleOrNull
        val total = envelope.raw["total_gb"]?.jsonPrimitive?.doubleOrNull
        val percent = envelope.raw["percent"]?.jsonPrimitive?.doubleOrNull
        if (used == null || total == null || percent == null) return null
        return QuotaTileReading(usedGb = used, totalGb = total, percent = percent)
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
